"""Manual live retrieval (network) of the multisource layers over Akanda. Not part of pytest.

* WorldCover v200: COG window by HTTP Range (reused if already stored);
* OpenStreetMap: one bounded Overpass query (reused if a snapshot < 7 days exists);
* Sentinel-1: CDSE monthly IW mosaic for the month of the S2 date; header-only
  estimate first, then VV and VH windows only if the estimate is under the cap.

Nothing existing is overwritten; Sentinel-2 acquisitions are not touched.
Prints network bytes per host, files written, peak memory. Run from repo root:
    .venv/Scripts/python.exe scripts/fetch_multisource_akanda_live.py [--month 2025-01] [--s1-cap-mb 16]
"""

import argparse
from datetime import date
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import geopandas as gpd
import psutil

from src.config import DATA_DIR
from src.data.external_http import ExternalHttpClient
from src.data.layer_store import FileLayerRepository, LayerFileStore
from src.data.network import SecureStacIO
from src.data.openstreetmap import fetch_osm_snapshot
from src.data.sentinel1 import estimate_window_bytes, fetch_sentinel1_window, search_mosaics
from src.data.worldcover import fetch_worldcover_window
from src.network_config import NetworkSettings


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--month", default="2025-01")
    parser.add_argument("--s1-cap-mb", type=float, default=16.0)
    parser.add_argument("--skip-osm", action="store_true")
    args = parser.parse_args()
    started, process = time.perf_counter(), psutil.Process()
    aoi_path = DATA_DIR / "aoi" / "akanda.geojson"
    bounds = tuple(float(v) for v in gpd.read_file(aoi_path).to_crs("EPSG:4326").total_bounds)
    store = LayerFileStore(DATA_DIR / "external")
    repository = FileLayerRepository(store)
    before = {key for key in store.keys("")} if store.root.exists() else set()
    settings = NetworkSettings.from_environment()
    client = ExternalHttpClient(settings)
    report = {"aoi_bounds_wgs84": bounds}

    _, descriptor, wc = fetch_worldcover_window(client, store, repository, aoi_id="akanda", wgs84_bounds=bounds)
    report["worldcover"] = {**wc, "product": descriptor.product, "sha256": descriptor.quality.checksum_sha256}

    if not args.skip_osm:
        frame, descriptor, osm = fetch_osm_snapshot(client, store, repository, wgs84_bounds=bounds)
        report["openstreetmap"] = {**osm, "osm_base_utc": descriptor.provenance["osm_base_utc"],
                                   "features": len(frame),
                                   "rejected": {k: descriptor.provenance[k] for k in descriptor.provenance
                                                if k.startswith("rejected_")},
                                   "groups": frame["group"].value_counts().to_dict()}

    year, month = (int(part) for part in args.month.split("-"))
    stac = SecureStacIO(settings)
    mosaics, rejected, truncated = search_mosaics(stac, bounds, date(year, month, 1), date(year, month, 28))
    report["sentinel1_search"] = {"items": [m.item_id for m in mosaics], "rejected": rejected, "truncated": truncated}
    if mosaics:
        from src.data.ingestion import create_cdse_s3_client

        mosaic = mosaics[-1]
        s3 = create_cdse_s3_client()
        cap = int(args.s1_cap_mb * 1024 * 1024)
        for polarization in ("VV", "VH"):
            key = f"sentinel1/{mosaic.item_id}_{polarization}_akanda.tif"
            entry = {"asset_size_bytes": mosaic.assets[polarization].size_bytes, "cap_bytes": cap}
            if not store.exists(key):
                estimate, header = estimate_window_bytes(s3, mosaic, polarization, bounds)
                entry.update(estimated_tile_bytes=estimate.compressed_bytes, tiles=estimate.tiles,
                             header_bytes=header, window=f"{estimate.width}x{estimate.height}")
                if estimate.compressed_bytes + header * 2 > cap:
                    entry["status"] = "refusé : estimation supérieure au plafond"
                    report[f"sentinel1_{polarization}"] = entry
                    continue
            _, descriptor, fetched = fetch_sentinel1_window(s3, store, repository, mosaic, polarization,
                                                            aoi_id="akanda", wgs84_bounds=bounds, max_bytes=cap)
            entry.update(fetched, status="ok", sha256=descriptor.quality.checksum_sha256)
            report[f"sentinel1_{polarization}"] = entry
    after = set(store.keys(""))
    report["new_files"] = {key: store.path(key).stat().st_size for key in sorted(after - before)}
    report["external_http_usage"] = client.usage_summary()
    report["peak_rss_mb"] = round(process.memory_info().peak_wset / 1e6 if hasattr(process.memory_info(), "peak_wset")
                                  else process.memory_info().rss / 1e6, 1)
    report["elapsed_s"] = round(time.perf_counter() - started, 1)
    print(json.dumps(report, ensure_ascii=False, indent=1, default=str))


if __name__ == "__main__":
    main()
