"""Deterministic local benchmark for Sentinel-2 temporal-pair cache reuse.

Creates three small synthetic SCL rasters and one partial AOI outside the
timed region. Each selection evaluates all three pairs. The uncached
reference disables cache insertions for that call only; production API and
implementation remain unchanged. Caches are local to one selection call, so
the measured reuse is intra-call, never warm across invocations.
"""

from __future__ import annotations

from collections import Counter
from math import isclose
from pathlib import Path
from statistics import median
from tempfile import TemporaryDirectory
from time import perf_counter
from unittest.mock import patch

import geopandas as gpd
import numpy as np
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import box

import src.geo.pair_selection as selector


REPEATS = 7
WIDTH = HEIGHT = 64
PIXEL_SIZE_M = 10.0


def _create_synthetic_inputs(directory: Path):
    """Write fixed synthetic data once, before any timed selection."""
    transform = from_origin(0, HEIGHT * PIXEL_SIZE_M, PIXEL_SIZE_M, PIXEL_SIZE_M)
    rows, columns = np.indices((HEIGHT, WIDTH))
    classes = np.array([4, 5, 6, 7, 8, 9, 10, 0], dtype=np.uint8)
    paths = {}
    items = []
    for index, product_id in enumerate(("SYNTH_A", "SYNTH_B", "SYNTH_C")):
        values = classes[(rows * 3 + columns + index * 2) % len(classes)]
        path = directory / f"{product_id}_SCL.tif"
        with rasterio.open(
            path,
            "w",
            driver="GTiff",
            width=WIDTH,
            height=HEIGHT,
            count=1,
            dtype="uint8",
            crs="EPSG:6933",
            transform=transform,
            nodata=0,
        ) as dataset:
            dataset.write(values, 1)
        paths[product_id] = path
        items.append({
            "id": product_id,
            "properties": {
                "datetime": f"2025-01-{index * 10 + 1:02d}T10:00:00Z",
                "eo:cloud_cover": float(index * 5),
            },
        })

    aoi_path = directory / "synthetic_partial_aoi.gpkg"
    aoi = box(5, 5, WIDTH * PIXEL_SIZE_M - 5, HEIGHT * PIXEL_SIZE_M - 5)
    gpd.GeoDataFrame({"geometry": [aoi]}, crs="EPSG:6933").to_file(
        aoi_path, driver="GPKG"
    )
    quality = {
        product_id: {
            "usable_land_percent": 100.0,
            "aoi_coverage_percent": 100.0,
            "aoi_data_coverage_percent": 100.0,
        }
        for product_id in paths
    }
    config = selector.PairSelectionConfig(
        scl_policy="usable_land",
        min_interval_days=1,
        max_interval_days=30,
        max_acquisitions=3,
        max_pair_evaluations=3,
    )
    return items, paths, quality, aoi_path, config


def _assert_scientifically_equal(actual, expected):
    """Require identical discrete outputs and tight tolerances for areas."""
    assert actual.eligible_acquisitions == expected.eligible_acquisitions
    assert actual.evaluated_pairs == expected.evaluated_pairs == 3
    assert dict(actual.rejected_counts) == dict(expected.rejected_counts)
    assert actual.raster_failures == expected.raster_failures
    assert actual.acquisition_limit_reached is expected.acquisition_limit_reached
    assert actual.pair_limit_reached is expected.pair_limit_reached
    assert actual.search_scope is expected.search_scope
    assert len(actual.pairs) == len(expected.pairs)

    exact_fields = (
        "earlier_product_id",
        "later_product_id",
        "earlier_datetime",
        "later_datetime",
        "interval_days",
        "common_usable_pixels",
        "min_usable_land_percent",
        "min_aoi_coverage_percent",
        "min_aoi_data_coverage_percent",
        "cloud_cover_sum_percent",
        "suitability",
    )
    for actual_pair, expected_pair in zip(actual.pairs, expected.pairs):
        for field in exact_fields:
            assert getattr(actual_pair, field) == getattr(expected_pair, field)
        assert isclose(
            actual_pair.common_area_m2,
            expected_pair.common_area_m2,
            rel_tol=1e-12,
            abs_tol=1e-8,
        )
        assert isclose(
            actual_pair.common_coverage_percent,
            expected_pair.common_coverage_percent,
            rel_tol=1e-12,
            abs_tol=1e-10,
        )


def _run_once(items, paths, quality, aoi_path, config, *, cache_enabled):
    counters = Counter()
    durations = Counter()
    original_weights = selector.prepare_pixel_area_weights
    original_masks = selector.scl_policy_mask_on_grid

    def timed_call(name, function, *args, **kwargs):
        start = perf_counter()
        result = function(*args, **kwargs)
        durations[name] += perf_counter() - start
        counters[name] += 1
        return result

    def timed_weights(*args, **kwargs):
        return timed_call("weights", original_weights, *args, **kwargs)

    def timed_masks(*args, **kwargs):
        return timed_call("scl_alignments", original_masks, *args, **kwargs)

    patches = [
        patch.object(selector, "prepare_pixel_area_weights", side_effect=timed_weights),
        patch.object(selector, "scl_policy_mask_on_grid", side_effect=timed_masks),
    ]
    if not cache_enabled:
        patches.append(
            patch.object(selector, "_bounded_cache_put", side_effect=lambda *args: None)
        )

    with patches[0], patches[1]:
        if len(patches) == 3:
            with patches[2]:
                start = perf_counter()
                result = selector.select_temporal_pairs(
                    items,
                    scl_paths=paths,
                    quality_by_product=quality,
                    aoi_path=aoi_path,
                    config=config,
                    search_scope=selector.PairSearchScope.LOCAL_SUBSET,
                )
                total_seconds = perf_counter() - start
        else:
            start = perf_counter()
            result = selector.select_temporal_pairs(
                items,
                scl_paths=paths,
                quality_by_product=quality,
                aoi_path=aoi_path,
                config=config,
                search_scope=selector.PairSearchScope.LOCAL_SUBSET,
            )
            total_seconds = perf_counter() - start
    return total_seconds, counters, durations, result


def main():
    repository_root = Path(__file__).resolve().parents[1]
    with TemporaryDirectory(
        prefix=".geonaf-s2-cache-bench-", dir=repository_root
    ) as temporary_directory:
        inputs = _create_synthetic_inputs(Path(temporary_directory))

        # Warm Python imports and the operating-system file cache equally;
        # discard these calls. Selector caches are newly created each call.
        cached_warmup = _run_once(*inputs, cache_enabled=True)
        uncached_warmup = _run_once(*inputs, cache_enabled=False)
        _assert_scientifically_equal(cached_warmup[3], uncached_warmup[3])

        samples = {
            "cache_active": {"total": [], "weights": [], "scl_alignments": []},
            "cache_neutralized": {"total": [], "weights": [], "scl_alignments": []},
        }
        reference_result = None
        for repetition in range(REPEATS):
            modes = (True, False) if repetition % 2 == 0 else (False, True)
            for cache_enabled in modes:
                total, counts, durations, result = _run_once(
                    *inputs, cache_enabled=cache_enabled
                )
                mode = "cache_active" if cache_enabled else "cache_neutralized"
                samples[mode]["total"].append(total)
                for metric in ("weights", "scl_alignments"):
                    samples[mode][metric].append(durations[metric])
                assert counts["weights"] >= 1
                assert counts["scl_alignments"] >= 1
                if reference_result is None:
                    reference_result = result
                else:
                    _assert_scientifically_equal(result, reference_result)

        cached = _run_once(*inputs, cache_enabled=True)
        uncached = _run_once(*inputs, cache_enabled=False)
        _assert_scientifically_equal(cached[3], uncached[3])
        assert cached[3].evaluated_pairs == 3
        assert len(cached[3].pairs) == 3
        assert cached[1]["scl_alignments"] < uncached[1]["scl_alignments"]
        assert cached[1]["weights"] < uncached[1]["weights"]

        print("GEONAF Sentinel-2 pair-selection cache benchmark")
        print(f"Synthetic setup: {WIDTH}x{HEIGHT}, EPSG:6933, partial AOI; setup excluded")
        print(f"Evaluated pairs per call: {cached[3].evaluated_pairs}; repetitions: {REPEATS}")
        print("Fresh selector caches per call; reuse measured only within each call.")
        print("Timings are medians after one discarded warm-up per mode.")
        print()
        print("mode                 total_ms  weights_ms  weight_calls  scl_ms  scl_calls")
        for mode, display in (("cache_active", "cache actif"), ("cache_neutralized", "cache neutralisé")):
            representative = _run_once(*inputs, cache_enabled=(mode == "cache_active"))
            print(
                f"{display:20} "
                f"{median(samples[mode]['total']) * 1000:8.3f} "
                f"{median(samples[mode]['weights']) * 1000:10.3f} "
                f"{representative[1]['weights']:12d} "
                f"{median(samples[mode]['scl_alignments']) * 1000:7.3f} "
                f"{representative[1]['scl_alignments']:10d}"
            )
        cached_ms = median(samples["cache_active"]["total"]) * 1000
        uncached_ms = median(samples["cache_neutralized"]["total"]) * 1000
        speedup = uncached_ms / cached_ms if cached_ms else float("inf")
        print(f"\nSpeedup (neutralisé / actif): {speedup:.3f}x")
        print(f"Measured gain: {uncached_ms - cached_ms:.3f} ms ({(1 - cached_ms / uncached_ms) * 100:.1f}%)")
        print("Scientific outputs: identical (discrete exact; area/coverage within stated tolerances).")


if __name__ == "__main__":
    main()
