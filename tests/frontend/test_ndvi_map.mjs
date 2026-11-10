import test from "node:test";
import assert from "node:assert/strict";
import { mountNdviMap } from "../../src/components/maplibre/map.js";

function environment() {
  const elements = new Map();
  const root = { dataset: {}, removed: false,
    querySelector(selector) {
      if (!elements.has(selector)) elements.set(selector, { style: {}, textContent: "" });
      return elements.get(selector);
    }, remove() { this.removed = true; } };
  globalThis.document = { createElement: () => root };
  globalThis.ResizeObserver = class { observe() {} disconnect() { root.disconnected = true; } };
  class FakeMap {
    constructor(options) { this.options = options; this.events = {}; this.sources = {}; this.layers = {}; }
    on(name, callback) { this.events[name] = callback; }
    once(name, callback) { this.events[name] = callback; }
    addControl() {}
    addSource(id, value) { this.sources[id] = value; }
    addLayer(value) { this.layers[value.id] = value; }
    getLayer(id) { return this.layers[id]; }
    setLayoutProperty(id, key, value) { this.visibility = value; }
    fitBounds(value) { this.bounds = value; }
    resize() {}
    remove() { this.removed = true; }
  }
  const library = { Map: FakeMap, NavigationControl: class {}, ScaleControl: class {} };
  const payload = { schema_version: "geonaf.ndvi-map/v1", image: "data:image/png;base64,AA==",
    acquired_at: "2025-01-25T09:31:59Z", coordinates: [[9, 1], [10, 1], [10, 0], [9, 0]],
    legend: [{ color: "#ff0000" }, { color: "#00ff00" }], aoi: {}, footprint: {} };
  return { root, library, payload, container: { appendChild() {} } };
}

test("standalone map keeps interactions local and releases WebGL resources", () => {
  const { root, library, payload, container } = environment();
  let failures = 0;
  const view = mountNdviMap(container, payload, library, () => failures++);
  view.map.events.load();
  assert.deepEqual(view.map.options.style.sources, {}); // No external basemap.
  assert.equal(view.map.sources.ndvi.url, payload.image);
  assert.deepEqual(view.map.sources.ndvi.coordinates, payload.coordinates);
  assert.equal(view.map.layers.ndvi.paint["raster-resampling"], "nearest");
  view.map.events.idle();
  assert.equal(root.dataset.status, "ready");
  view.map.events.mousemove({ lngLat: { lng: 9.123456, lat: 0.567891 } });
  assert.match(root.querySelector(".geonaf-map-coordinates").textContent, /9\.12346.*0\.56789/);
  root.querySelector(".geonaf-layer-toggle").onchange({ target: { checked: false } });
  assert.equal(view.map.visibility, "none");
  root.querySelector(".geonaf-map-reset").onclick();
  assert.deepEqual(view.map.bounds, [[9, 0], [10, 1]]);
  assert.equal(failures, 0);
  view.dispose();
  assert.equal(root.removed && root.disconnected && view.map.removed, true);
});

test("two-finger map gestures only on touch screens", () => {
  for (const coarse of [false, true]) {
    globalThis.matchMedia = query => ({ matches: coarse && query === "(pointer: coarse)" });
    const { library, payload, container } = environment();
    const view = mountNdviMap(container, payload, library, () => {});
    assert.equal(view.map.options.cooperativeGestures, coarse);
    view.dispose();
  }
  delete globalThis.matchMedia;
});

test("missing WebGL reports a safe failure once", () => {
  const { root, library, payload, container } = environment();
  library.Map = class { constructor() { throw new Error("WebGL missing"); } };
  let failures = 0;
  const view = mountNdviMap(container, payload, library, () => failures++);
  assert.equal(root.dataset.status, "error");
  assert.equal(failures, 1);
  assert.doesNotMatch(root.querySelector(".geonaf-map-status").textContent, /WebGL missing/);
  view.dispose();
});
