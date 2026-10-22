"""Manual offline metadata benchmark; no network, pixels or asset downloads.

Run from the repository root: python -B scripts/benchmark_acquisition_catalogue.py
Reports aggregate metrics only. AOI loading is outside the measured repetitions.
"""

from datetime import date, datetime, timezone
import json
from pathlib import Path
import statistics
import sys
from time import perf_counter
import tracemalloc
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import DATA_DIR
from src.data.catalogue import CatalogueQuery, CatalogueRecord, CatalogueResult
from src.services.acquisition_catalogue import load_catalogue_aoi, search_catalogue, select_acquisitions, catalogue_rows


def main():
    count = 10_000
    query = CatalogueQuery(load_catalogue_aoi(DATA_DIR, "akanda"), date(2025, 1, 1), date(2025, 1, 31))
    stamp = datetime(2025, 1, 15, tzinfo=timezone.utc)
    records = tuple(CatalogueRecord("sentinel-2-l2a", f"synthetic-{index:05}", stamp, 25.0, True, True, True)
                    for index in range(count))
    class Repository:
        def search(self, request):
            return CatalogueResult(request, records, True, count, "synthetic-offline", stamp)
    elapsed = []
    tracemalloc.start()
    with patch("rasterio.open", side_effect=AssertionError("No raster reads in catalogue")):
        for _ in range(3):
            start = perf_counter()
            result = search_catalogue(query, DATA_DIR, repository=Repository())
            rows = catalogue_rows(result)
            selection = select_acquisitions(result, [row.stac_id for row in result.records[:100]])
            elapsed.append((perf_counter() - start) * 1000)
            assert len(rows) == count and len(selection.records) == 100
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    print(json.dumps({"records": count, "repetitions": 3, "median_ms": statistics.median(elapsed),
                      "tracemalloc_peak_bytes": peak, "public_json_bytes": len(json.dumps(rows).encode()),
                      "raster_reads": 0, "network_calls": 0, "asset_downloads": 0,
                      "scope": "local metadata scan, record decoration, public rows and selection; not STAC or browser latency"}, indent=2))


if __name__ == "__main__":
    main()
