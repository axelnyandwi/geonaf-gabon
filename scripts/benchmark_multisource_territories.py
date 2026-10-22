"""Manual offline benchmark of the multisource engine for Akanda, Libreville and Owendo.

Uses only stored local data (no network). Reports per territory: cold timings of
each step, browser payload size, coverages and peak memory. Run from repo root:
    .venv/Scripts/python.exe scripts/benchmark_multisource_territories.py
"""

import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import geopandas as gpd
import psutil

from src.config import DATA_DIR
from src.data.openstreetmap import load_osm_snapshot
from src.geo.scientific_profile import standard_configuration
from src.services.external_layers import latest_osm, open_store, stored_layers
from src.services.local_preview import analyze_local_group, group_local_observations, list_local_observations
from src.services.multisource_demo import analyze_multisource
from src.services.multisource_map import prepare_multisource_map
from src.services.sentinel1_layer import load_radar_mosaic


def main():
    process = psutil.Process()
    groups = group_local_observations(list_local_observations(DATA_DIR))
    group = next(g for g in groups if g[0].acquired_at.date().isoformat() == "2025-01-25")
    store, repository = open_store(DATA_DIR)
    report = {}
    for aoi_id in ("akanda", "libreville", "owendo"):
        timings, started = {}, time.perf_counter()
        aoi = DATA_DIR / "aoi" / f"{aoi_id}.geojson"
        bounds = tuple(float(v) for v in gpd.read_file(aoi).to_crs("EPSG:4326").total_bounds)
        profile = standard_configuration(aoi_id).effective
        observation = analyze_local_group(group, DATA_DIR, scientific_profile=profile)
        timings["ndvi_s"] = time.perf_counter() - started
        mark = time.perf_counter()
        wc = [layer for layer in stored_layers(repository, "esa-worldcover", bounds)
              if layer.descriptor.provenance.get("aoi_id") == aoi_id]
        osm_layer = latest_osm(repository, bounds)
        osm = load_osm_snapshot(store, osm_layer.storage_key, bounds) if osm_layer else None
        timings["osm_load_s"] = time.perf_counter() - mark
        mark = time.perf_counter()
        radar_layers = [layer for layer in stored_layers(repository, "sentinel-1-mosaic", bounds)
                        if layer.descriptor.provenance.get("aoi_id") == aoi_id]
        radar = tuple(load_radar_mosaic([(l.path, l.descriptor) for l in radar_layers
                                         if l.descriptor.provenance["polarization"] == pol], aoi_path=aoi,
                                        landcover_path=[l.path for l in wc], landcover_classes=dict(wc[0].descriptor.classes))
                      for pol in ("VV", "VH") if any(l.descriptor.provenance["polarization"] == pol for l in radar_layers))
        timings["radar_s"] = time.perf_counter() - mark
        mark = time.perf_counter()
        result = analyze_multisource(observation, aoi_path=aoi, worldcover=[(l.path, l.descriptor) for l in wc],
                                     osm=osm, radar=radar)
        timings["multisource_s"] = time.perf_counter() - mark
        mark = time.perf_counter()
        payload = prepare_multisource_map(result, aoi_path=aoi, radar_polarization="VV")
        timings["map_payload_s"] = time.perf_counter() - mark
        encoded = json.dumps(payload).encode()
        report[aoi_id] = {
            **{k: round(v, 2) for k, v in timings.items()}, "total_cold_s": round(time.perf_counter() - started, 2),
            "payload_mb": round(len(encoded) / 1e6, 2),
            "payload_parts_mb": {r["id"]: round(len(r["image"]) / 1e6, 2) for r in payload["rasters"]},
            "vectors": len(payload["vectors"]["features"]) if payload["vectors"] else 0,
            "coverage": {s.source: None if s.coverage_percent is None else round(s.coverage_percent, 2)
                         for s in result.sources},
            "ndvi_median": round(observation.summary.median, 4), "missing": list(result.missing),
            "osm_features": int(osm_layer.descriptor.provenance["features"]) if osm_layer else 0,
            "rss_mb": round(process.memory_info().rss / 1e6)}
    report["peak_mb"] = round(getattr(process.memory_info(), "peak_wset", process.memory_info().rss) / 1e6)
    print(json.dumps(report, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
