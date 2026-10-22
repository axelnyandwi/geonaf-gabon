import test from "node:test";
import assert from "node:assert/strict";
import { mountMultisourceMap } from "../../src/components/maplibre/multisource.js";

class Node {
  constructor(tag) {
    this.tag = tag; this.children = []; this.dataset = {}; this.style = {}; this.attributes = {};
    this._text = ""; this.className = "";
  }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map(c => c.textContent).join(""); }
  get innerHTML() { throw new Error("innerHTML must not be used"); }
  set innerHTML(value) { throw new Error("innerHTML must not be used"); }
  append(...nodes) { this.children.push(...nodes); }
  appendChild(node) { this.children.push(node); }
  replaceChildren(...nodes) { this.children = nodes; }
  setAttribute(name, value) { this.attributes[name] = value; }
  remove() { this.removed = true; }
  all(predicate, output = []) {
    if (predicate(this)) output.push(this);
    for (const child of this.children) if (child instanceof Node) child.all(predicate, output);
    return output;
  }
}

function environment(overrides = {}) {
  globalThis.document = { createElement: tag => new Node(tag), createTextNode: text => ({ textContent: text }) };
  globalThis.ResizeObserver = class { observe() {} disconnect() {} };
  class FakeMap {
    constructor(options) { this.options = options; this.events = {}; this.layerEvents = {}; this.sources = {}; this.layers = {}; }
    on(name, layer, callback) {
      if (callback) this.layerEvents[`${name}:${layer}`] = callback; else this.events[name] = layer;
    }
    once(name, callback) { this.events[name] = callback; }
    addControl() {}
    addSource(id, value) { this.sources[id] = value; }
    addLayer(value) { this.layers[value.id] = value; }
    getLayer(id) { return this.layers[id]; }
    setLayoutProperty(id, key, value) { this.layers[id].layout = { ...(this.layers[id].layout || {}), [key]: value }; }
    getStyle() { return { layers: Object.values(this.layers) }; }
    fitBounds(value) { this.bounds = value; }
    resize() {}
    remove() { this.removed = true; }
  }
  const library = { Map: FakeMap, NavigationControl: class {}, ScaleControl: class {} };
  const legend = { kind: "classes", items: [{ color: "#006400", label: "Couvert arboré" }] };
  const payload = {
    schema_version: "geonaf.multisource-map/v1", coordinates: [[9, 1], [10, 1], [10, 0], [9, 0]],
    rasters: [
      { id: "ndvi", label: "NDVI", nature: "Observation mesurée", image: "data:image/png;base64,AA==", visible: true,
        opacity: 0.9, legend: { kind: "gradient", low: "−1", high: "+1", colors: ["#a50026", "#006837"], extra: [] } },
      { id: "worldcover", label: "WorldCover", nature: "Classification externe", image: "data:image/png;base64,AA==",
        visible: false, opacity: 0.7, legend },
    ],
    vectors: { type: "FeatureCollection", features: [{ type: "Feature", geometry: { type: "LineString", coordinates: [[9, 0], [9.1, 0.1]] },
      properties: { group: "Routes et voies", category: "highway", detail: "<img src=x onerror=alert(1)>", osm_id: "way/1", color: "#facc15" } }] },
    vector_legend: [{ color: "#facc15", label: "Routes et voies" }],
    aoi: { type: "Feature", properties: {}, geometry: { type: "Polygon", coordinates: [] } },
    attributions: ["© OpenStreetMap contributors", "Contains modified Copernicus Sentinel data [2025]"],
    note: "Affichage reprojeté.", ...overrides,
  };
  const container = new Node("div");
  return { library, payload, container };
}

test("multisource map toggles layers, shows legends and attributions, without external basemap", () => {
  const { library, payload, container } = environment();
  const view = mountMultisourceMap(container, payload, library);
  view.map.events.load();
  assert.deepEqual(view.map.options.style.sources, {});
  assert.equal(view.map.layers.ndvi.paint["raster-resampling"], "nearest");
  assert.equal(view.map.layers.worldcover.layout.visibility, "none");
  const root = container.children[0];
  const inputs = root.all(node => node.tag === "input");
  assert.deepEqual(inputs.map(input => input.dataset.layer), ["ndvi", "worldcover", "osm"]);
  const worldcover = inputs[1];
  worldcover.checked = true;
  worldcover.onchange();
  assert.equal(view.map.layers.worldcover.layout.visibility, "visible");
  assert.match(root.textContent, /Couvert arboré/);
  assert.match(root.textContent, /© OpenStreetMap contributors/);
  view.map.events.idle();
  assert.equal(root.dataset.status, "ready");
  view.dispose();
  assert.ok(view.map.removed && root.removed);
});

test("contributed OSM text is rendered as text, never as HTML", () => {
  const { library, payload, container } = environment();
  const view = mountMultisourceMap(container, payload, library);
  view.map.events.load();
  view.map.layerEvents["click:osm-line"]({ features: [{ properties: payload.vectors.features[0].properties }] });
  const root = container.children[0];
  assert.match(root.textContent, /<img src=x onerror=alert\(1\)>/); // Literal text, no element created.
  assert.equal(root.all(node => node.tag === "img").length, 0);
});

test("invalid payloads are rejected", () => {
  const { library, container } = environment();
  assert.throws(() => mountMultisourceMap(container, { schema_version: "other", rasters: [] }, library));
  const { payload } = environment({ rasters: [{ id: "x", image: "javascript:alert(1)" }] });
  assert.throws(() => mountMultisourceMap(container, payload, library));
});
