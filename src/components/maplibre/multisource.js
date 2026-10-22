/** Standalone multisource MapLibre view (no Streamlit import; reusable from React).
 * Rasters share one set of corners; OSM vectors are GeoJSON. Contributed text
 * (OSM tags) is always inserted with textContent, never as HTML.
 */
export function mountMultisourceMap(container, payload, maplibre, onError = () => {}) {
  if (payload.schema_version !== "geonaf.multisource-map/v1" ||
      !payload.rasters.every(r => r.image.startsWith("data:image/png;base64,"))) {
    throw new Error("Invalid multisource payload");
  }
  const el = (tag, cls, text) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  };
  const root = el("section", "gms-shell");
  root.dataset.geonafMultisource = "true";
  const toolbar = el("div", "gms-toolbar");
  const panel = el("div", "gms-layers");
  panel.setAttribute("aria-label", "Couches");
  const reset = el("button", "gms-reset", "Recentrer sur l’AOI");
  reset.type = "button";
  toolbar.append(panel, reset);
  const canvas = el("div", "gms-canvas");
  canvas.setAttribute("aria-label", "Carte multisource interactive");
  const legend = el("div", "gms-legend");
  const info = el("div", "gms-info", "Cliquer sur un objet OSM pour afficher ses attributs.");
  info.setAttribute("aria-live", "polite");
  const coords = el("div", "gms-coords", "Longitude / latitude : déplacer le pointeur");
  const status = el("div", "gms-status", "Chargement de la carte…");
  status.setAttribute("role", "status");
  const attribution = el("div", "gms-attribution", payload.attributions.join(" · "));
  root.append(toolbar, canvas, legend, info, coords, status, attribution);
  container.appendChild(root);

  const c = payload.coordinates;
  const bounds = [[c[3][0], c[3][1]], [c[1][0], c[1][1]]];
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
      center: [(bounds[0][0] + bounds[1][0]) / 2, (bounds[0][1] + bounds[1][1]) / 2],
      zoom: 11, attributionControl: false,
    });
  } catch (error) {
    fail();
    return { dispose: () => root.remove() };
  }
  root.__geonafMap = map;
  map.addControl(new maplibre.NavigationControl({ showCompass: false }), "top-right");
  map.addControl(new maplibre.ScaleControl({ unit: "metric" }));
  map.on("error", fail);

  const state = {};
  const drawLegend = () => {
    legend.replaceChildren();
    for (const raster of payload.rasters) {
      if (!state[raster.id]) continue;
      const block = el("div", "gms-legend-block");
      block.append(el("strong", "", `${raster.label} — ${raster.nature}`));
      if (raster.legend.kind === "gradient") {
        const row = el("div", "gms-legend-row");
        const bar = el("span", "gms-gradient");
        bar.style.background = `linear-gradient(to right, ${raster.legend.colors.join(", ")})`;
        row.append(el("span", "", raster.legend.low), bar, el("span", "", raster.legend.high));
        block.append(row);
      }
      for (const item of (raster.legend.items || []).concat(raster.legend.extra || [])) {
        const row = el("div", "gms-legend-row");
        const swatch = el("span", "gms-swatch");
        swatch.style.background = item.color;
        row.append(swatch, el("span", "", item.label));
        block.append(row);
      }
      legend.append(block);
    }
    if (state.osm && payload.vector_legend.length) {
      const block = el("div", "gms-legend-block");
      block.append(el("strong", "", "OpenStreetMap — Base collaborative"));
      for (const item of payload.vector_legend) {
        const row = el("div", "gms-legend-row");
        const swatch = el("span", "gms-swatch");
        swatch.style.background = item.color;
        row.append(swatch, el("span", "", item.label));
        block.append(row);
      }
      legend.append(block);
    }
  };
  const toggle = (id, layers, label, checked) => {
    const wrapper = el("label", "gms-toggle");
    const input = document.createElement("input");
    input.type = "checkbox";
    input.checked = checked;
    input.dataset.layer = id;
    state[id] = checked;
    input.onchange = () => {
      state[id] = input.checked;
      for (const layer of layers) {
        if (map.getLayer(layer)) map.setLayoutProperty(layer, "visibility", input.checked ? "visible" : "none");
      }
      drawLegend();
    };
    wrapper.append(input, document.createTextNode(" " + label));
    panel.append(wrapper);
  };
  map.on("load", () => {
    for (const raster of payload.rasters) {
      map.addSource(raster.id, { type: "image", url: raster.image, coordinates: c });
      map.addLayer({ id: raster.id, type: "raster", source: raster.id,
        layout: { visibility: raster.visible ? "visible" : "none" },
        paint: { "raster-resampling": "nearest", "raster-fade-duration": 0, "raster-opacity": raster.opacity } });
      toggle(raster.id, [raster.id], raster.label, raster.visible);
    }
    if (payload.vectors && payload.vectors.features.length) {
      map.addSource("osm", { type: "geojson", data: payload.vectors });
      map.addLayer({ id: "osm-fill", type: "fill", source: "osm", filter: ["match", ["geometry-type"], ["Polygon", "MultiPolygon"], true, false],
        paint: { "fill-color": ["get", "color"], "fill-opacity": 0.25, "fill-outline-color": ["get", "color"] } });
      map.addLayer({ id: "osm-line", type: "line", source: "osm",
        paint: { "line-color": ["get", "color"], "line-width": 1.4 } });
      toggle("osm", ["osm-fill", "osm-line"], "OSM : voies, eau, usages", true);
      for (const layer of ["osm-fill", "osm-line"]) {
        map.on("click", layer, event => {
          const p = event.features[0].properties;
          info.textContent = `${p.group} · ${p.category}${p.detail ? " = " + p.detail : ""} · ${p.osm_id} ` +
            "(donnée contributive, date de terrain inconnue)";
        });
      }
    }
    map.addSource("aoi", { type: "geojson", data: payload.aoi });
    map.addLayer({ id: "aoi", type: "line", source: "aoi", paint: { "line-color": "#ffffff", "line-width": 2 } });
    drawLegend();
    map.fitBounds(bounds, { padding: 25, duration: 0 });
    map.once("idle", () => {
      if (failed) return;
      root.dataset.status = "ready";
      status.textContent = "Contour blanc : AOI. Sans fond de carte externe. " + payload.note;
    });
  });
  map.on("mousemove", event => {
    coords.textContent = `Longitude : ${event.lngLat.lng.toFixed(5)} · Latitude : ${event.lngLat.lat.toFixed(5)} (WGS84)`;
  });
  reset.onclick = () => map.fitBounds(bounds, { padding: 25, duration: 0 });
  const resize = new ResizeObserver(() => map.resize());
  resize.observe(root);
  return { map, dispose: () => { resize.disconnect(); map.remove(); root.remove(); } };
}
