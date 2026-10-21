/** Run in the generated offline Akanda fixture: await runTemporalBrowserChecks().
 * These checks use the real MapLibre engine, not the unit-test double.
 */
export async function runTemporalBrowserChecks() {
  const demo = window.temporalDemo;
  const check = (condition, message) => { if (!condition) throw new Error(message); };
  const frame = () => new Promise(resolve => requestAnimationFrame(resolve));
  const stable = async () => {
    const start = performance.now();
    while (!demo.view?.maps.every(map => map.loaded() && !map.isMoving())) {
      check(performance.now()-start < 15000, "Map did not stabilize");
      await frame();
    }
    await frame();
  };
  await demo.ready();
  const initial = demo.inspect();
  const mounts = [], disposed = [];
  for (let i = 0; i < 3; i++) {
    demo.dispose(); await frame();
    const released = demo.inspect();
    check(released.canvasCount === 0 && released.webgl.live === 0, "WebGL resources remain after disposal");
    check(released.diagnostics.mapsCreated === released.diagnostics.mapsRemoved, "Unbalanced map disposal");
    disposed.push({ liveContexts: released.webgl.live, canvases: released.canvasCount, heapBytes: released.heapBytes });
    demo.mount(); await demo.ready(); await stable();
    const mounted = demo.inspect();
    mounts.push({ mountMs: mounted.diagnostics.mountMs, readyMs: mounted.diagnostics.readyMs,
      activeMaps: mounted.diagnostics.activeMaps, liveContexts: mounted.webgl.live, heapBytes: mounted.heapBytes });
  }
  let start = performance.now();
  demo.view.maps[0].jumpTo({ center: [9.4, .52], zoom: 12.2, bearing: 23, pitch: 40 });
  await stable();
  const cameraStableMs = performance.now()-start;
  const cameras = demo.inspect().camera;
  check(cameras.every(camera => JSON.stringify(camera) === JSON.stringify(cameras[0])), "Cameras diverged");
  const updates = demo.view.diagnostics.cameraUpdates;
  for (let i = 0; i < 20; i++) await frame();
  check(demo.view.diagnostics.cameraUpdates === updates, "Camera feedback loop");
  const select = document.querySelector(".temporal-change");
  start = performance.now(); select.value = "delta"; select.dispatchEvent(new Event("change"));
  await demo.ready(); await stable();
  const deltaStableMs = performance.now()-start;
  const changeView = demo.view.maps[demo.view.maps.length-1];
  check(changeView.getPaintProperty("observation", "raster-resampling") === "nearest", "Unexpected interpolation");
  select.value = "classes"; select.dispatchEvent(new Event("change")); await demo.ready(); await stable();
  const result = demo.inspect();
  check(result.failures === 0 && result.diagnostics.contextLosses === 0, "Rendering error");
  check(!result.resources.some(r => /^https?:/i.test(r.name)), "External request");
  check(result.scientificDigest === "b0356911623b4af85939f9a5bb62f0f01cdff9ffd1dae875ca84b77523cb0e41", "Scientific identity changed");
  const text = document.querySelector(".geonaf-temporal").textContent;
  check(!/asset_paths|aoi_path|CDSE_S3|OneDrive/.test(text), "Private metadata rendered");
  return { initial: initial.diagnostics, mounts, disposed, cameraStableMs, deltaStableMs,
    cameras, feedbackLoop: false, failures: result.failures, externalRequests: 0,
    resourceEntries: result.resources.length, scientificDigest: result.scientificDigest,
    htmlNetworkBackend: false, finalWebgl: result.webgl,
    viewport: {width: innerWidth, height: innerHeight}, browser: navigator.userAgent,
    memoryLimit: "performance.memory is coarse; native/GPU allocation totals are not measured" };
}
