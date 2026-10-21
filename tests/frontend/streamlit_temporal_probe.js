/** Manual DevTools init script for the real localhost Streamlit application.
 * Records counts and sizes only; never stores WebSocket contents or credentials.
 */
(() => {
  const nativeSocket = window.WebSocket;
  const traffic = { receivedBytes: 0, sentBytes: 0, receivedMessages: 0, sentMessages: 0 };
  const size = data => typeof data === "string" ? new TextEncoder().encode(data).length : data.byteLength ?? data.size ?? 0;
  window.WebSocket = class extends nativeSocket {
    constructor(...args) {
      super(...args);
      this.addEventListener("message", event => {
        traffic.receivedBytes += size(event.data); traffic.receivedMessages++;
      });
    }
    send(data) { traffic.sentBytes += size(data); traffic.sentMessages++; return super.send(data); }
  };
  const contexts = [], known = new WeakSet();
  const originalContext = HTMLCanvasElement.prototype.getContext;
  HTMLCanvasElement.prototype.getContext = function(type, ...args) {
    const context = originalContext.call(this, type, ...args);
    if (context && /webgl/.test(type) && !known.has(context)) {
      known.add(context); contexts.push(new WeakRef(context));
    }
    return context;
  };
  function walk(root) {
    const nodes = [root, ...root.querySelectorAll("*")];
    for (const node of nodes) {
      if (node.__geonafTemporalView) return { view: node.__geonafTemporalView, parent: node };
      if (node.shadowRoot) { const found = walk(node.shadowRoot); if (found) return found; }
    }
    return null;
  }
  const mounts = [], seen = new WeakSet();
  const timer = setInterval(() => {
    const current = walk(document);
    if (current?.view.diagnostics.status === "ready" && !seen.has(current.view)) {
      seen.add(current.view);
      mounts.push({ readyAtMs: performance.now(), diagnostics: current.view.diagnostics });
    }
  }, 20);
  window.geonafProbe = {
    get current() { return walk(document); },
    async ready() {
      const started = performance.now();
      while (walk(document)?.view.diagnostics.status !== "ready") {
        if (performance.now()-started > 60000) throw Error("Temporal Streamlit map not ready");
        await new Promise(resolve => setTimeout(resolve, 20));
      }
      await new Promise(resolve => setTimeout(resolve, 30));
      return this.read();
    },
    read() {
      const current = walk(document);
      return { traffic: { ...traffic }, mounts: [...mounts], diagnostics: current?.view.diagnostics,
        camera: current?.view.maps.map(map => ({ center: map.getCenter().toArray(), zoom: map.getZoom(), bearing: map.getBearing(), pitch: map.getPitch() })),
        webgl: { created: contexts.length, live: contexts.filter(r => r.deref() && !r.deref().isContextLost()).length },
        heapBytes: performance.memory?.usedJSHeapSize ?? null,
        resources: performance.getEntriesByType("resource").map(entry => ({
          host: entry.name.startsWith("http") ? new URL(entry.name).host : entry.name.split(":")[0],
          transferBytes: entry.transferSize, decodedBytes: entry.decodedBodySize })),
        viewport: { width: innerWidth, height: innerHeight } };
    },
    stop() { clearInterval(timer); },
  };
})();
