/** Standalone geonaf.temporal-map/v1 renderer. No Python/Streamlit bridge.
 * Navigation and display toggles remain entirely within this module.
 */
export function validateTemporalPayload(payload) {
  const ids = ["ndvi_initial", "ndvi_final", "delta", "classes"];
  const png = value => typeof value === "string" && /^data:image\/png;base64,[A-Za-z0-9+/]+={0,2}$/.test(value);
  const grid = payload?.grid;
  if (payload?.schema_version !== "geonaf.temporal-map/v1" ||
      !grid || grid.crs !== "EPSG:3857" ||
      ![grid.width, grid.height].every(n => Number.isInteger(n) && n > 0 && n <= 2048) ||
      grid.coordinates?.length !== 4 || !grid.coordinates.every(p =>
        Array.isArray(p) && p.length === 2 && p.every(Number.isFinite) && Math.abs(p[0]) <= 180 && Math.abs(p[1]) <= 85.052) ||
      payload.layers?.length !== 4 || !ids.every(id => payload.layers.filter(l => l.id === id).length === 1)) {
    throw new Error("Invalid temporal map contract");
  }
  for (const layer of payload.layers) {
    if (!png(layer.image) || !png(payload.masks?.[layer.mask_id]) ||
        (layer.legend_image !== undefined && !png(layer.legend_image)) ||
        !Array.isArray(layer.legend) || !layer.legend.length ||
        !layer.legend.every(stop => /^#[0-9a-f]{6}$/i.test(stop.color))) {
      throw new Error("Invalid temporal image or legend");
    }
  }
  for (const id of ids.slice(0, 2)) {
    const layer = payload.layers.find(l => l.id === id);
    if (JSON.stringify(layer.range) !== "[-1,1]" || layer.kind !== "continuous") {
      throw new Error("NDVI scales must be identical");
    }
  }
  const initialLegend = payload.layers.find(l => l.id === ids[0]).legend;
  const finalLegend = payload.layers.find(l => l.id === ids[1]).legend;
  const classes = payload.layers.find(l => l.id === "classes");
  const delta = payload.layers.find(l => l.id === "delta");
  if (JSON.stringify(initialLegend) !== JSON.stringify(finalLegend) ||
      classes.kind !== "categorical" || ![1, 2, 3].every(code => classes.legend.filter(s => s.code === code).length === 1) ||
      delta.kind !== "continuous" || JSON.stringify(delta.range) !== "[-2,2]" ||
      payload.mask_policy?.scl_policy !== "usable_land" ||
      !Number.isFinite(payload.classification?.threshold) || payload.classification.threshold <= 0) {
    throw new Error("Invalid scientific display metadata");
  }
  const geometryIsPublic = feature => feature?.type === "Feature" &&
    ["Polygon", "MultiPolygon"].includes(feature.geometry?.type) &&
    Array.isArray(feature.geometry.coordinates);
  if (!geometryIsPublic(payload.aoi) || !geometryIsPublic(payload.footprint) ||
      !["initial", "final"].every(key => {
        const p = payload.provenance?.[key];
        return typeof p?.product_id === "string" && typeof p?.acquired_at_utc === "string" &&
          Number.isFinite(Date.parse(p.acquired_at_utc));
      })) throw new Error("Invalid public provenance or geometry");
}

/** Synchronizes the full camera. A scoped guard blocks synchronous feedback;
 * equality blocks delayed move events once target cameras already agree.
 */
export function synchronizeCameras(maps, onCamera = () => {}) {
  let syncing = false;
  const bindings = [];
  const cameraOf = map => ({ center: map.getCenter().toArray(), zoom: map.getZoom(),
    bearing: map.getBearing(), pitch: map.getPitch() });
  const same = (a, b) => [...a.center, a.zoom, a.bearing, a.pitch].every((n, i) =>
    Math.abs(n - [...b.center, b.zoom, b.bearing, b.pitch][i]) < 1e-9);
  for (const source of maps) {
    const listener = () => {
      if (syncing) return;
      syncing = true;
      try {
        const camera = cameraOf(source);
        onCamera(camera);
        for (const target of maps) {
          if (target !== source && !same(camera, cameraOf(target))) target.jumpTo(camera);
        }
      } finally { syncing = false; }
    };
    source.on("move", listener);
    bindings.push([source, listener]);
  }
  return () => { for (const [map, listener] of bindings) map.off("move", listener); };
}

export function mountTemporalMap(container, payload, maplibre, { onError = () => {} } = {}) {
  validateTemporalPayload(payload);
  const started = performance.now();
  const root = document.createElement("section");
  root.className = "geonaf-temporal";
  root.dataset.status = "loading";
  root.innerHTML = `<div class="temporal-controls">
    <label>Vue mobile <select class="temporal-active"><option value="initial">NDVI initial</option>
    <option value="final">NDVI final</option><option value="change">Changement</option></select></label>
    <label>Mode de changement <select class="temporal-change"><option value="classes">Changements par catégories</option>
    <option value="delta">Intensité des changements (ΔNDVI)</option></select></label>
    <button type="button" class="temporal-reset">Recentrer</button></div>
    <div class="temporal-mode-help" role="note"><p class="temporal-mode-summary"></p>
    <details><summary>Couleurs, seuils et limites scientifiques</summary><div class="temporal-mode-details"></div></details></div>
    <p class="temporal-policy">Dates : observations individuelles SCL 4/5. Changement : uniquement la comparaison valide aux deux dates.
    Gris : indisponible ; transparent : extérieur AOI. Aperçu ; aucune statistique calculée sur les images.</p>
    <div class="temporal-panels"></div>
    <p class="temporal-coordinates" aria-live="off">Coordonnées WGS84 : déplacer le pointeur</p>
    <details><summary>Provenance publique</summary><pre class="temporal-provenance"></pre></details>
    <p class="temporal-status" role="status">Chargement local…</p>`;
  container.appendChild(root);
  const panels = root.querySelector(".temporal-panels");
  const status = root.querySelector(".temporal-status");
  const pick = (value, keys) => Object.fromEntries(keys.filter(key => value?.[key] !== undefined).map(key => [key, value[key]]));
  const publicAcquisition = acquisition => ({ ...pick(acquisition, ["product_id", "acquired_at_utc"]),
    calibration: pick(acquisition.calibration, ["red_offset", "nir_offset", "quantification_value"]) });
  root.querySelector(".temporal-provenance").textContent = JSON.stringify({
    initial: publicAcquisition(payload.provenance.initial), final: publicAcquisition(payload.provenance.final),
    source_grid: pick(payload.provenance.source_grid, ["crs", "width", "height", "transform", "resolution"]),
    scientific: { ...pick(payload.provenance.scientific, ["interpretation_status", "seasonal_status", "season_relation", "aoi_sha256", "persistent_change_validation"]),
      profile: pick(payload.provenance.scientific?.profile, ["profile_id", "version", "fingerprint", "aoi_id", "experimental"]) },
    classification: { ...pick(payload.classification, ["threshold", "delta_order"]),
      rules: pick(payload.classification.rules, ["0", "1", "2", "3"]) },
    mask_policy: { ...pick(payload.mask_policy, ["scl_policy", "scl_classes", "aoi_pixel_rule", "masked_rgba"]),
      categories: pick(payload.mask_policy.categories, ["0", "1", "2"]) }, result_id: payload.result_id,
  }, null, 2);
  const media = window.matchMedia("(max-width: 760px)");
  let disposed = false, failed = false, changeMode = "classes", active = "initial";
  let entries = [], disconnectSync = () => {}, generation = 0, lastCamera = null;
  let transitionStarted = started;
  let livePayload = payload;
  payload = null; // Only livePayload retains the potentially large images.
  const metrics = { mountMs: 0, readyMs: null, rebuildReadyMs: null, cameraUpdates: 0,
    mapsCreated: 0, mapsRemoved: 0, contextLosses: 0 };
  const domCleanups = [];
  const listen = (element, event, fn) => {
    element.addEventListener(event, fn);
    domCleanups.push(() => element.removeEventListener(event, fn));
  };
  function fail() {
    if (disposed || failed) return;
    failed = true;
    root.dataset.status = "error";
    status.textContent = "Carte interactive indisponible ; repli Matplotlib à utiliser par l’appelant.";
    destroyMaps();
    onError(); // Error notification only; never contains raw SDK exceptions.
  }
  function destroyMaps() {
    generation++;
    disconnectSync(); disconnectSync = () => {};
    for (const entry of entries) {
      for (const [name, fn] of entry.events) entry.map.off(name, fn);
      entry.canvas?.removeEventListener("webglcontextlost", entry.contextLost);
      entry.map.remove();
      metrics.mapsRemoved++;
    }
    entries = [];
    panels.replaceChildren();
  }
  function layerFor(view) {
    const id = view === "change" ? changeMode : view === "initial" ? "ndvi_initial" : "ndvi_final";
    return livePayload.layers.find(layer => layer.id === id);
  }
  function updateModeHelp() {
    const threshold = livePayload.classification.threshold;
    const details = root.querySelector('.temporal-mode-details');
    details.replaceChildren();
    const summary = root.querySelector('.temporal-mode-summary');
    summary.textContent = changeMode === 'classes'
      ? `Changements par catégories : baisse, sous le seuil ou hausse, avec un seuil de ±${threshold} en ΔNDVI.`
      : 'Intensité des changements (ΔNDVI) : valeur finale moins valeur initiale, sur une échelle fixe de −2 à +2.';
    const paragraph = text => { const p = document.createElement('p'); p.textContent = text; details.append(p); };
    if (changeMode === 'classes') {
      const layer = layerFor('change');
      for (const [code, text] of [[1, `Baisse : ΔNDVI < −${threshold}`],
        [2, `Sous le seuil : −${threshold} ≤ ΔNDVI ≤ +${threshold} (bornes incluses)`],
        [3, `Hausse : ΔNDVI > +${threshold}`]]) {
        const color = layer.legend.find(stop => stop.code === code).color;
        const row = document.createElement('p'); const swatch = document.createElement('i');
        swatch.className = 'temporal-swatch'; swatch.style.backgroundColor = color;
        row.append(swatch, document.createTextNode(`${text} · ${color}`)); details.append(row);
      }
      paragraph('Sous le seuil ne signifie pas absence de variation. Ces seuils décrivent un indice ; ils ne mesurent pas une significativité statistique et ne sont pas universels selon les écosystèmes.');
    } else {
      paragraph('Palette RdYlGn : rouge pour les valeurs négatives (baisse), jaune pâle autour de zéro, vert pour les valeurs positives (hausse). La bande de légende reprend les 256 couleurs réellement utilisées par les images.');
      paragraph(`Ce mode ne découpe pas les couleurs selon ±${threshold} : ces seuils s’appliquent au mode par catégories. Changer de mode ne modifie ni les calculs ni les indicateurs.`);
    }
    paragraph('Gris : absence de comparaison valide aux deux dates ; ce n’est ni un ΔNDVI nul ni une catégorie stable. Transparent : extérieur de l’AOI. Les NDVI individuels gardent le masque propre à chaque acquisition.');
    paragraph('Une variation du NDVI ne démontre pas une déforestation. Saison, humidité, marées, nuages résiduels et saturation de l’indice peuvent influencer le signal. Les zones observables ne représentent pas nécessairement toute l’AOI.');
  }
  function describe(entry) {
    const layer = layerFor(entry.view);
    const p = livePayload.provenance;
    entry.title.textContent = entry.view === "change"
      ? `${changeMode === 'classes' ? 'Changements par catégories' : 'Intensité des changements (ΔNDVI)'} · ${p.initial.acquired_at_utc.slice(0, 10)} → ${p.final.acquired_at_utc.slice(0, 10)}`
      : `NDVI ${entry.view === "initial" ? "initial" : "final"} · ${p[entry.view].acquired_at_utc.slice(0, 10)}`;
    entry.legend.replaceChildren();
    if (layer.kind === "continuous") {
      const gradient = document.createElement(layer.legend_image ? 'img' : 'span');
      gradient.className = "temporal-gradient";
      if (layer.legend_image) {
        gradient.src = layer.legend_image;
        gradient.alt = 'Palette RdYlGn réellement utilisée : rouge, jaune pâle, vert';
      } else {
        // Compatibility with previously prepared v1 payloads.
        gradient.style.background = `linear-gradient(to right, ${layer.legend.map(s => s.color).join(", ")})`;
      }
      const caption = document.createElement("span");
      caption.textContent = `${entry.view === "change" ? "ΔNDVI" : "NDVI"} : ${layer.range[0]} à ${layer.range[1]}`;
      entry.legend.append(caption, gradient);
    } else {
      const t = livePayload.classification.threshold;
      for (const [code, label] of [[1, `Baisse Δ < −${t}`], [2, `Sous le seuil −${t} ≤ Δ ≤ ${t}`], [3, `Hausse Δ > ${t}`]]) {
        const item = document.createElement("span");
        const swatch = document.createElement("i");
        swatch.style.backgroundColor = layer.legend.find(s => s.code === code).color;
        swatch.className = "temporal-swatch";
        item.append(swatch, document.createTextNode(label)); entry.legend.append(item);
      }
    }
    const meaning = document.createElement("span");
    meaning.textContent = entry.view === "change" ? "Gris : non comparable aux deux dates" : "Gris : observation masquée à cette date";
    entry.legend.append(meaning);
  }
  function buildMaps() {
    if (disposed || failed) return;
    const rebuildStart = performance.now();
    transitionStarted = rebuildStart;
    destroyMaps();
    const currentGeneration = generation;
    root.dataset.status = "loading";
    const views = media.matches ? [active] : ["initial", "final", "change"];
    root.dataset.layout = media.matches ? "single" : "triple";
    const corners = livePayload.grid.coordinates;
    const bounds = [corners[3], corners[1]];
    const camera = lastCamera || { center: [(corners[0][0]+corners[2][0])/2, (corners[0][1]+corners[2][1])/2],
      zoom: 11, bearing: 0, pitch: 0 };
    try {
      for (const view of views) {
        const panel = document.createElement("article");
        const title = document.createElement("h3");
        const canvas = document.createElement("div"); canvas.className = "temporal-canvas";
        canvas.setAttribute("aria-label", `Carte ${view}`);
        const legend = document.createElement("div"); legend.className = "temporal-legend";
        panel.append(title, canvas, legend); panels.append(panel);
        const map = new maplibre.Map({ container: canvas, ...camera,
          style: { version: 8, sources: {}, layers: [{ id: "background", type: "background",
            paint: { "background-color": "#0e1117" } }] }, attributionControl: false,
          // Touch screens: one finger scrolls the page, two fingers move the map.
          cooperativeGestures: typeof matchMedia === "function" && matchMedia("(pointer: coarse)").matches,
          // Defensive allowlist: no HTTP(S), relative URL, remote style, font or tile.
          transformRequest: url => {
            if (!url.startsWith("data:image/png;base64,")) throw new Error("External map resource forbidden");
            return { url };
          },
        });
        metrics.mapsCreated++;
        const entry = { map, view, title, legend, events: [], ready: false };
        entries.push(entry); describe(entry);
        const on = (name, fn) => { map.on(name, fn); entry.events.push([name, fn]); };
        entry.canvas = map.getCanvas();
        entry.contextLost = () => { metrics.contextLosses++; fail(); };
        entry.canvas.addEventListener("webglcontextlost", entry.contextLost);
        on("error", fail);
        on("mousemove", event => {
          root.querySelector(".temporal-coordinates").textContent =
            `Longitude : ${event.lngLat.lng.toFixed(5)} · Latitude : ${event.lngLat.lat.toFixed(5)} (WGS84)`;
        });
        on("load", () => {
          if (disposed || currentGeneration !== generation) return;
          try {
            const layer = layerFor(view);
            map.addSource("observation", { type: "image", url: layer.image, coordinates: corners });
            map.addLayer({ id: "observation", type: "raster", source: "observation",
              paint: { "raster-resampling": "nearest", "raster-fade-duration": 0 } });
            for (const [id, color] of [["footprint", "#60a5fa"], ["aoi", "#ffffff"]]) {
              // Drop arbitrary Feature properties even on a forged input object.
              map.addSource(id, { type: "geojson", data: { type: "Feature", properties: {}, geometry: livePayload[id].geometry } });
              map.addLayer({ id, type: "line", source: id,
                paint: { "line-color": color, "line-width": id === "aoi" ? 2 : 1,
                  ...(id === "footprint" ? { "line-dasharray": [3, 2] } : {}) } });
            }
          } catch { fail(); }
        });
        on("idle", () => {
          if (disposed || failed || currentGeneration !== generation || !map.getLayer("observation")) return;
          entry.ready = true;
          if (entries.every(item => item.ready) && root.dataset.status !== "ready") {
            root.dataset.status = "ready";
            metrics.readyMs ??= performance.now()-started;
            metrics.rebuildReadyMs = performance.now()-transitionStarted;
            root.dataset.readyMs = String(metrics.readyMs);
            status.textContent = "Copernicus Sentinel-2 · contour blanc : AOI ; pointillés bleus : emprise raster. Aucun fond externe.";
          }
        });
        map.addControl(new maplibre.NavigationControl(), "top-right");
        map.addControl(new maplibre.ScaleControl({ unit: "metric" }));
      }
      disconnectSync = synchronizeCameras(entries.map(e => e.map), camera => {
        metrics.cameraUpdates++; lastCamera = camera;
      });
      if (!lastCamera) entries[0].map.fitBounds(bounds, { padding: 20, duration: 0 });
    } catch { fail(); }
  }
  listen(root.querySelector(".temporal-change"), "change", event => {
    changeMode = event.target.value;
    updateModeHelp();
    for (const entry of entries.filter(e => e.view === "change")) {
      describe(entry);
      const source = entry.map.getSource("observation");
      if (source) {
        transitionStarted = performance.now();
        root.dataset.status = "loading"; entry.ready = false;
        source.updateImage({ url: layerFor("change").image, coordinates: livePayload.grid.coordinates });
      }
    }
  });
  listen(root.querySelector(".temporal-active"), "change", event => {
    active = event.target.value;
    if (media.matches) buildMaps();
  });
  listen(root.querySelector(".temporal-reset"), "click", () => {
    entries[0]?.map.fitBounds([livePayload.grid.coordinates[3], livePayload.grid.coordinates[1]], { padding: 20, duration: 0 });
  });
  const mediaListener = () => buildMaps();
  media.addEventListener("change", mediaListener);
  const resize = new ResizeObserver(() => { for (const entry of entries) entry.map.resize(); });
  resize.observe(root);
  updateModeHelp();
  buildMaps();
  metrics.mountMs = performance.now()-started;
  return {
    get maps() { return entries.map(e => e.map); },
    get diagnostics() { return { ...metrics, activeMaps: entries.length, disposed, status: root.dataset.status }; },
    dispose() {
      if (disposed) return;
      disposed = true; resize.disconnect(); media.removeEventListener("change", mediaListener);
      for (const cleanup of domCleanups) cleanup();
      domCleanups.length = 0; destroyMaps(); livePayload = null; root.remove();
    },
  };
}
