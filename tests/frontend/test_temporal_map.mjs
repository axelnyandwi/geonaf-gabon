import test from "node:test";
import assert from "node:assert/strict";
import { mountTemporalMap, synchronizeCameras, validateTemporalPayload } from "../../src/components/maplibre/temporal.js";

class Element {
  constructor() { this.children = []; this.events = new Map(); this.elements = new Map(); this.dataset = {}; this.style = {}; }
  append(...elements) { this.children.push(...elements); }
  appendChild(element) { this.append(element); }
  replaceChildren(...elements) { this.children = elements; }
  querySelector(selector) {
    if (!this.elements.has(selector)) this.elements.set(selector, new Element());
    return this.elements.get(selector);
  }
  setAttribute() {}
  addEventListener(name, fn) { if (!this.events.has(name)) this.events.set(name, new Set()); this.events.get(name).add(fn); }
  removeEventListener(name, fn) { this.events.get(name)?.delete(fn); }
  emit(name, value = {}) { for (const fn of [...(this.events.get(name) || [])]) fn(value); }
  remove() { this.removed = true; }
}

class FakeMap extends Element {
  constructor(options) {
    super(); this.options = options; this.camera = { center: options.center, zoom: options.zoom, bearing: options.bearing || 0, pitch: options.pitch || 0 };
    this.sources = {}; this.layers = {}; this.canvas = new Element(); this.jumps = 0;
  }
  on(name, fn) { this.addEventListener(name, fn); }
  off(name, fn) { this.removeEventListener(name, fn); }
  getCenter() { return { toArray: () => this.camera.center }; }
  getZoom() { return this.camera.zoom; }
  getBearing() { return this.camera.bearing; }
  getPitch() { return this.camera.pitch; }
  jumpTo(camera) { this.camera = structuredClone(camera); this.jumps++; this.emit("move"); }
  fitBounds(bounds) { this.jumpTo({ center: [(bounds[0][0]+bounds[1][0])/2, (bounds[0][1]+bounds[1][1])/2], zoom: 10, bearing: 0, pitch: 0 }); }
  addSource(id, source) { this.sources[id] = { ...source, updateImage: update => { Object.assign(this.sources[id], update); } }; }
  getSource(id) { return this.sources[id]; }
  addLayer(layer) { this.layers[layer.id] = layer; }
  getLayer(id) { return this.layers[id]; }
  addControl() {}
  getCanvas() { return this.canvas; }
  resize() { this.resized = true; }
  remove() { this.removed = true; this.sources = {}; this.layers = {}; }
}

function fixture(mobile = false, MapClass = FakeMap) {
  globalThis.document = { createElement: () => new Element(), createTextNode: text => ({ textContent: text }) };
  const media = new Element(); media.matches = mobile;
  globalThis.window = { matchMedia: () => media };
  let observer;
  globalThis.ResizeObserver = class { constructor(fn) { observer = this; this.fn = fn; } observe() {} disconnect() { this.disconnected = true; } };
  const image = "data:image/png;base64,AA==";
  const legend = [{ value: -1, color: "#ff0000" }, { value: 1, color: "#00ff00" }];
  const payload = { schema_version: "geonaf.temporal-map/v1", result_id: "public-id",
    grid: { width: 2, height: 2, crs: "EPSG:3857", coordinates: [[9, 1], [10, 1], [10, 0], [9, 0]] },
    layers: ["ndvi_initial", "ndvi_final", "delta", "classes"].map((id, index) => ({ id, image,
      kind: id === "classes" ? "categorical" : "continuous", mask_id: index < 2 ? ["initial", "final"][index] : "comparable",
      range: index < 2 ? [-1, 1] : [-2, 2], legend: id === "classes" ?
        [1, 2, 3].map(code => ({ code, color: "#112233" })) : legend,
      ...(id !== 'classes' ? {legend_image: image} : {}) })),
    masks: { initial: image, final: image, comparable: image },
    aoi: { type: "Feature", properties: { private: "do-not-forward" }, geometry: { type: "Polygon", coordinates: [] } },
    footprint: { type: "Feature", geometry: { type: "Polygon", coordinates: [] } },
    mask_policy: { scl_policy: "usable_land" }, classification: { threshold: .1 },
    provenance: { initial: { product_id: "public-initial", acquired_at_utc: "2025-01-15T09:30:00Z" },
      final: { product_id: "public-final", acquired_at_utc: "2025-01-25T09:30:00Z" } },
  };
  const library = { Map: MapClass, NavigationControl: class {}, ScaleControl: class {} };
  const container = new Element();
  return { payload, library, container, media, observer: () => observer };
}

function load(view) {
  for (const map of view.maps) map.emit("load");
  for (const map of view.maps) map.emit("idle");
}

test("full cameras synchronize without recursive or delayed-event feedback", () => {
  const maps = [1, 2, 3].map(() => new FakeMap({ center: [9, 1], zoom: 10 }));
  let updates = 0;
  const disconnect = synchronizeCameras(maps, () => updates++);
  maps[0].jumpTo({ center: [9.3, .6], zoom: 13, bearing: 30, pitch: 45 });
  for (const map of maps) assert.deepEqual(map.camera, maps[0].camera);
  assert.equal(updates, 1);
  assert.deepEqual(maps.map(m => m.jumps), [1, 1, 1]);
  maps[1].emit("move"); maps[2].emit("move");
  assert.deepEqual(maps.map(m => m.jumps), [1, 1, 1]);
  disconnect(); maps[0].jumpTo({ center: [10, 1], zoom: 8, bearing: 0, pitch: 0 });
  assert.notDeepEqual(maps[0].camera, maps[1].camera);
});

test("three views share coordinates/scales; change toggle stays local and sources are public", () => {
  const f = fixture(); let errors = 0;
  f.payload.provenance.initial.asset_paths = { private: "do-not-forward" };
  const view = mountTemporalMap(f.container, f.payload, f.library, { onError: () => errors++ });
  load(view);
  const root = f.container.children[0];
  assert.equal(view.maps.length, 3); assert.equal(root.dataset.status, "ready");
  assert.ok(!root.querySelector(".temporal-provenance").textContent.includes("do-not-forward"));
  for (const map of view.maps) {
    assert.deepEqual(map.sources.observation.coordinates, f.payload.grid.coordinates);
    assert.equal(map.layers.observation.paint["raster-resampling"], "nearest");
    assert.deepEqual(map.sources.aoi.data.properties, {});
    assert.throws(() => map.options.transformRequest("https://external.invalid/image.png"));
    assert.throws(() => map.options.transformRequest("/private-data"));
    assert.deepEqual(map.options.style.sources, {});
  }
  root.querySelector(".temporal-change").emit("change", { target: { value: "delta" } });
  assert.equal(view.maps.length, 3);
  assert.equal(view.maps[2].sources.observation.url, f.payload.layers[2].image);
  view.maps[2].emit("idle"); assert.equal(root.dataset.status, "ready");
  view.maps[0].emit("mousemove", { lngLat: { lng: 9.123456, lat: .567891 } });
  assert.match(root.querySelector(".temporal-coordinates").textContent, /9\.12346.*0\.56789/);
  assert.equal(errors, 0); view.dispose();
});

test("mobile keeps one map, retains camera across views and restores desktop", () => {
  const f = fixture(true); const view = mountTemporalMap(f.container, f.payload, f.library);
  load(view); const root = f.container.children[0]; const old = view.maps[0];
  old.jumpTo({ center: [9.4, .5], zoom: 14, bearing: 17, pitch: 42 });
  root.querySelector(".temporal-active").emit("change", { target: { value: "change" } });
  assert.equal(view.maps.length, 1); assert.equal(old.removed, true);
  assert.deepEqual(view.maps[0].camera, old.camera);
  load(view); f.media.matches = false; f.media.emit("change"); load(view);
  assert.equal(view.maps.length, 3);
  for (const map of view.maps) assert.deepEqual(map.camera, old.camera);
  view.dispose();
});

test('mode help tracks thresholds, exact palette strip and local switching', () => {
  const f = fixture(); const view = mountTemporalMap(f.container, f.payload, f.library);
  load(view); const root = f.container.children[0];
  const summary = root.querySelector('.temporal-mode-summary');
  assert.match(summary.textContent, /Changements par catégories.*±0,10 en ΔNDVI/);  // French decimal comma.
  const firstLegend = root.querySelector('.temporal-panels').children[0].children[2];
  assert.equal(firstLegend.children[1].src, f.payload.layers[0].legend_image);
  const oldCamera = structuredClone(view.maps[0].camera);
  root.querySelector('.temporal-change').emit('change', {target: {value: 'delta'}});
  assert.match(summary.textContent, /Intensité des changements.*−2 à \+2/);
  assert.ok(root.querySelector('.temporal-mode-details').children.some(p => /256 couleurs/.test(p.textContent || '')));
  assert.equal(view.diagnostics.mapsCreated, 3);
  assert.deepEqual(view.maps[0].camera, oldCamera);
  root.querySelector('.temporal-change').emit('change', {target: {value: 'classes'}});
  assert.match(summary.textContent, /±0,10/);
  view.dispose();
});

test("dispose releases map and DOM listeners, media/resize observers and is idempotent", () => {
  const f = fixture(); const view = mountTemporalMap(f.container, f.payload, f.library);
  load(view); const maps = view.maps; const root = f.container.children[0];
  view.dispose(); view.dispose();
  assert.equal(view.maps.length, 0);
  assert.equal(view.diagnostics.mapsCreated, view.diagnostics.mapsRemoved);
  assert.equal(root.removed, true); assert.equal(f.observer().disconnected, true);
  for (const map of maps) {
    assert.equal(map.removed, true);
    assert.ok([...map.events.values()].every(listeners => listeners.size === 0));
    assert.equal(map.canvas.events.get("webglcontextlost").size, 0);
  }
  assert.equal(f.media.events.get("change").size, 0);
  f.media.emit("change"); f.observer().fn(); assert.equal(view.maps.length, 0);
});

test("partial WebGL construction failure releases prior maps and reports only once", () => {
  let calls = 0; let first;
  class BrokenMap extends FakeMap {
    constructor(options) { if (++calls === 2) throw new Error("private failure"); super(options); first = this; }
  }
  const f = fixture(false, BrokenMap); let errors = 0;
  const view = mountTemporalMap(f.container, f.payload, f.library, { onError: () => errors++ });
  assert.equal(errors, 1); assert.equal(first.removed, true);
  assert.equal(view.diagnostics.status, "error"); assert.equal(view.maps.length, 0);
  view.dispose();
});

test("context loss reports a safe error and cleans all live maps", () => {
  const f = fixture(); let errors = 0;
  const view = mountTemporalMap(f.container, f.payload, f.library, { onError: () => errors++ });
  load(view); const map = view.maps[0]; map.canvas.emit("webglcontextlost");
  assert.equal(errors, 1); assert.equal(view.diagnostics.contextLosses, 1);
  assert.equal(view.maps.length, 0); view.dispose();
});

test("rejects remote images, mismatched scales, unknown schema and missing provenance before mount", () => {
  const f = fixture();
  for (const mutate of [p => p.schema_version = "other/v1", p => p.layers[0].image = "https://external.invalid/a.png",
    p => p.layers[0].range = [0, 1], p => p.layers[0].legend = [{ color: "red;url(https://external.invalid)" }],
    p => p.grid.coordinates[0][0] = NaN, p => delete p.provenance.final,
    p => p.layers[0].legend_image = 'https://external.invalid/legend.png']) {
    const payload = structuredClone(f.payload); mutate(payload);
    assert.throws(() => validateTemporalPayload(payload));
    assert.throws(() => mountTemporalMap(f.container, payload, f.library));
  }
  assert.equal(f.container.children.length, 0);
});
