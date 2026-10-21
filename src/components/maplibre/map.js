/** Standalone MapLibre view. No imports from Streamlit; reusable from React.
 * Call dispose() on unmount. All pan/zoom/coordinate events stay in JavaScript.
 */
export function mountNdviMap(container, payload, maplibre, onError = () => {}) {
  if (payload.schema_version !== "geonaf.ndvi-map/v1" ||
      !payload.image.startsWith("data:image/png;base64,")) {
    throw new Error("Invalid map payload");
  }
  const started = performance.now();
  const root = document.createElement("section");
  root.className = "geonaf-map-shell";
  root.dataset.geonafMap = "true";
  root.dataset.preparationMs = String(payload.preparation_ms);
  root.dataset.imageSize = `${payload.width}×${payload.height}`;
  root.innerHTML = `<div class="geonaf-map-toolbar">
    <strong class="geonaf-map-date"></strong>
    <label><input type="checkbox" class="geonaf-layer-toggle" checked> NDVI / masque</label>
    <button type="button" class="geonaf-map-reset">Recentrer sur l’AOI</button>
    </div><div class="geonaf-map-canvas" aria-label="Carte NDVI interactive Akanda"></div>
    <div class="geonaf-map-legend"><span>NDVI −1</span><span class="geonaf-gradient"></span><span>+1</span>
    <span class="geonaf-masked-key"></span><span>Observation masquée</span>
    <span>Fond sombre : extérieur de l’AOI</span></div>
    <div class="geonaf-map-coordinates" aria-live="polite">Longitude / latitude : déplacer le pointeur</div>
    <div class="geonaf-map-status" role="status">Chargement de la carte locale…</div>`;
  container.appendChild(root);
  root.querySelector(".geonaf-map-date").textContent =
    `NDVI · ${payload.acquired_at.slice(0, 10)} · SCL 4/5`;
  root.querySelector(".geonaf-gradient").style.background =
    `linear-gradient(to right, ${payload.legend.map(s => s.color).join(", ")})`;
  const coordinates = payload.coordinates;
  const bounds = [coordinates[3], coordinates[1]];
  let failed = false;
  const fail = () => {
    if (failed) return;
    failed = true;
    root.dataset.status = "error";
    root.querySelector(".geonaf-map-status").textContent =
      "Carte interactive indisponible ; utiliser les cartes Matplotlib.";
    onError();
  };
  let map;
  try {
    map = new maplibre.Map({
      container: root.querySelector(".geonaf-map-canvas"),
      style: { version: 8, sources: {}, layers: [
        { id: "background", type: "background", paint: { "background-color": "#0e1117" } },
      ] },
      center: [(bounds[0][0] + bounds[1][0]) / 2, (bounds[0][1] + bounds[1][1]) / 2],
      zoom: 11, attributionControl: false,
    });
  } catch (error) {
    fail();
    return { dispose: () => root.remove() };
  }
  root.__geonafMap = map; // Allows browser tests to exercise the real map instance.
  map.addControl(new maplibre.NavigationControl({ showCompass: false }), "top-right");
  map.addControl(new maplibre.ScaleControl({ unit: "metric" }));
  map.on("error", fail);
  map.on("load", () => {
    map.addSource("ndvi", { type: "image", url: payload.image, coordinates });
    map.addLayer({ id: "ndvi", type: "raster", source: "ndvi",
      paint: { "raster-resampling": "nearest", "raster-fade-duration": 0 } });
    map.addSource("footprint", { type: "geojson", data: payload.footprint });
    map.addLayer({ id: "footprint", type: "line", source: "footprint",
      paint: { "line-color": "#60a5fa", "line-width": 1, "line-dasharray": [3, 2] } });
    map.addSource("aoi", { type: "geojson", data: payload.aoi });
    map.addLayer({ id: "aoi", type: "line", source: "aoi",
      paint: { "line-color": "#ffffff", "line-width": 2 } });
    map.fitBounds(bounds, { padding: 25, duration: 0 });
    map.once("idle", () => {
      if (failed) return;
      root.dataset.status = "ready";
      root.dataset.readyMs = String(performance.now() - started);
      root.dataset.readyAt = String(performance.now());
      root.querySelector(".geonaf-map-status").textContent =
        "Copernicus Sentinel-2 · contour blanc : AOI ; pointillés bleus : emprise raster. Aperçu, sans fond externe.";
    });
  });
  map.on("mousemove", event => {
    root.querySelector(".geonaf-map-coordinates").textContent =
      `Longitude : ${event.lngLat.lng.toFixed(5)} · Latitude : ${event.lngLat.lat.toFixed(5)} (WGS84)`;
  });
  root.querySelector(".geonaf-layer-toggle").onchange = event => {
    if (map.getLayer("ndvi")) map.setLayoutProperty("ndvi", "visibility", event.target.checked ? "visible" : "none");
  };
  root.querySelector(".geonaf-map-reset").onclick = () => map.fitBounds(bounds, { padding: 25, duration: 0 });
  const resize = new ResizeObserver(() => map.resize());
  resize.observe(root);
  return { map, dispose: () => { resize.disconnect(); map.remove(); root.remove(); } };
}
