/** Standalone territory MapLibre view: exact outlines and comparison parts (no Streamlit import).
 * Labels are inserted with textContent, never as HTML. No external base map.
 */
export function mountTerritoryMap(container, payload, maplibre, onError = () => {}) {
  if (payload.schema_version !== "geonaf.territory-map/v1" || !Array.isArray(payload.layers)) {
    throw new Error("Invalid territory payload");
  }
  const el = (tag, cls, text) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  };
  const root = el("section", "gtm-shell");
  root.dataset.geonafTerritory = "true";
  const reset = el("button", "gtm-reset", "Recentrer");
  reset.type = "button";
  const canvas = el("div", "gtm-canvas");
  canvas.setAttribute("aria-label", "Carte des limites territoriales");
  const legend = el("div", "gtm-legend");
  const coords = el("div", "gtm-coords", "Longitude / latitude : déplacer le pointeur");
  const status = el("div", "gtm-status", "Chargement de la carte…");
  status.setAttribute("role", "status");
  const attribution = el("div", "gtm-attribution", (payload.attributions || []).join(" · "));
  root.append(reset, canvas, legend, coords, status, attribution);
  container.appendChild(root);

  for (const layer of payload.layers) {
    const row = el("span", "gtm-legend-row");
    const swatch = el("span", `gtm-swatch gtm-${layer.style}`);
    swatch.style.borderColor = layer.color;
    if (layer.style === "fill") swatch.style.background = layer.color;
    row.append(swatch, el("span", "", layer.label));
    legend.append(row);
  }
  const bounds = payload.bounds;
  let failed = false;
  const fail = () => {
    if (failed) return;
    failed = true;
    root.dataset.status = "error";
    status.textContent = "Carte interactive indisponible.";
    onError();
  };
  let map;
  try {
    map = new maplibre.Map({
      container: canvas,
      style: { version: 8, sources: {}, layers: [
        { id: "background", type: "background", paint: { "background-color": "#0e1117" } }] },
      bounds, fitBoundsOptions: { padding: 30 }, attributionControl: false,
      // Touch screens: one finger scrolls the page, two fingers move the map.
      cooperativeGestures: typeof matchMedia === "function" && matchMedia("(pointer: coarse)").matches,
    });
  } catch (error) {
    fail();
    return { dispose: () => root.remove() };
  }
  root.__geonafMap = map;
  map.addControl(new maplibre.NavigationControl({ showCompass: false }), "top-right");
  map.addControl(new maplibre.ScaleControl({ unit: "metric" }));
  map.on("error", fail);
  map.on("load", () => {
    for (const layer of payload.layers) {
      map.addSource(layer.id, { type: "geojson", data: layer.geojson });
      if (layer.style === "fill") {
        map.addLayer({ id: `${layer.id}-fill`, type: "fill", source: layer.id,
                       paint: { "fill-color": layer.color, "fill-opacity": 0.35 } });
      } else {
        map.addLayer({ id: `${layer.id}-line`, type: "line", source: layer.id,
                       paint: { "line-color": layer.color, "line-width": 2.5,
                                ...(layer.style === "dashed" ? { "line-dasharray": [3, 2] } : {}) } });
      }
    }
    map.fitBounds(bounds, { padding: 30, duration: 0 });
    map.once("idle", () => {
      if (failed) return;
      root.dataset.status = "ready";
      status.textContent = "Sans fond de carte externe. " + payload.note;
    });
  });
  map.on("mousemove", event => {
    coords.textContent = `Longitude : ${event.lngLat.lng.toFixed(5)} · Latitude : ${event.lngLat.lat.toFixed(5)} (WGS84)`;
  });
  reset.onclick = () => map.fitBounds(bounds, { padding: 30, duration: 0 });
  const resize = new ResizeObserver(() => map.resize());
  resize.observe(root);
  return { map, dispose: () => { resize.disconnect(); map.remove(); root.remove(); } };
}
