"""Offline surface benchmark; source rasters are read, never overwritten.

Three alternating repetitions compare the frozen pre-optimization algorithm,
empty process cache, warm cache, and cache saturated at both limits. Synthetic
AOIs exercise holes, concavities and multipart boundaries separately. Timings
exclude cache setup, imports, assertions and fingerprint construction.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from importlib.metadata import version
import json
from pathlib import Path
import platform
from statistics import median
import sys
from threading import Event, Thread
from time import perf_counter
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import numpy as np
from affine import Affine
from rasterio.crs import CRS
from shapely.geometry import box, Polygon, MultiPolygon

from area_weights_oracle import reference_cell_area_sums, reference_pixel_area_chunks
from scripts.benchmark_streamlit import scientific_digest
from src.config import DATA_DIR
from src.geo import change_analysis as area
from src.geo.temporal import RasterGrid
from src.services.local_preview import analyze_local_akanda, list_local_observations

REFERENCE = "995a1dbfe14c3ec6b1585411ffa59e180f23a151765dad5dff4d60fb354e073b"  # Découpage sans perte (docs/lossless_aoi_clip.md).
REPEATS = 3


def timed(function):
    """Sample process RSS, including native allocations, every 10 ms if available."""
    try:
        import psutil
    except ImportError:
        process = None
    else:
        process = psutil.Process()
    baseline = None if process is None else process.memory_info().rss
    samples = [] if baseline is None else [baseline]
    stop = Event()
    def sample():
        while not stop.wait(.01):
            samples.append(process.memory_info().rss)
    worker = None if process is None else Thread(target=sample, daemon=True)
    if worker:
        worker.start()
    started = perf_counter()
    try:
        result = function()
    finally:
        seconds = perf_counter() - started
        stop.set()
        if worker:
            worker.join()
            samples.append(process.memory_info().rss)
    return result, {"seconds": seconds, "rss_baseline_bytes": baseline,
                    "rss_peak_sampled_bytes": max(samples) if samples else None}


def summary(samples):
    seconds = [sample["seconds"] for sample in samples]
    return {"runs": samples, "median_seconds": median(seconds),
            "min_seconds": min(seconds), "max_seconds": max(seconds)}


def assert_sums_equal(actual, expected):
    np.testing.assert_allclose(actual[:2], expected[:2], rtol=1e-12, atol=1e-8)
    np.testing.assert_allclose(list(actual[2].values()), list(expected[2].values()),
                               rtol=1e-12, atol=1e-8)


def oracle_weights(grid, geometry):
    weights = np.empty((grid.height, grid.width), dtype=np.float64)
    for start, end, values in reference_pixel_area_chunks(grid, geometry):
        weights[start:end] = values
    return weights


def saturate(grid):
    """Four 16 MiB matrices fill exactly 64 MiB before the target request."""
    area.clear_area_weight_cache()
    filler = replace(grid, width=2048, height=1024)
    for i in range(4):
        geometry = box(-20_000_000-i, -9_000_000, 20_000_000, 9_000_000)
        # Private retrieval avoids making an unnecessary public ownership copy.
        area._cached_area_weights(filler, geometry)
    info = area.area_weight_cache_info()
    assert info["entries"] == 4
    assert info["bytes"] == 64*1024*1024
    return info


def scientific_check(result):
    digest = scientific_digest(result)
    if digest != REFERENCE:
        raise AssertionError("Scientific reference differs; explicit validation required.")
    return digest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="New public JSON report; never overwrite")
    args = parser.parse_args()
    observations = list_local_observations(DATA_DIR)
    if len(observations) != 2:
        raise ValueError("Two verified local observations are required.")
    area.clear_area_weight_cache()
    result = analyze_local_akanda(*observations, DATA_DIR)
    scientific_check(result)
    grid = result.common_grid
    geometry, denominator = area._load_aoi_equal_area(result.aoi_path)
    mask = result.analysis.comparable_mask
    classes = result.analysis.change_class_map
    expected = None
    surfaces = {name: [] for name in ("oracle", "cold", "warm", "saturated")}
    max_surface_delta = 0.
    max_coverage_delta = 0.
    saturation_before = []
    saturation_after = []
    for repetition in range(REPEATS):
        modes = ("oracle", "cold", "warm", "saturated")
        if repetition % 2:
            modes = tuple(reversed(modes))
        for mode in modes:
            area.clear_area_weight_cache()
            if mode == "warm":
                area._cell_area_sums(grid, mask, classes, geometry)
            elif mode == "saturated":
                saturation_before.append(saturate(grid))
            function = reference_cell_area_sums if mode == "oracle" else area._cell_area_sums
            actual, sample = timed(lambda: function(grid, mask, classes, geometry))
            sample["cache_after"] = area.area_weight_cache_info()
            if mode == "saturated":
                saturation_after.append(sample["cache_after"])
            surfaces[mode].append(sample)
            if expected is None:
                expected = actual
            assert_sums_equal(actual, expected)
            old_values = [*expected[:2], *expected[2].values()]
            new_values = [*actual[:2], *actual[2].values()]
            max_surface_delta = max(max_surface_delta, max(abs(a-b) for a,b in zip(old_values,new_values)))
            max_coverage_delta = max(max_coverage_delta, abs(actual[1]-expected[1])/denominator*100)
            assert max_coverage_delta <= 1e-9
            print(f"surface {repetition+1}/3 {mode}: {sample['seconds']:.6f}s", flush=True)

    # Compare every Akanda cell once, outside timed sections.
    old_weights = oracle_weights(grid, geometry)
    new_weights = area._compute_area_weights(grid, geometry)
    np.testing.assert_allclose(new_weights, old_weights, rtol=1e-12, atol=1e-8)
    np.testing.assert_array_equal(old_weights > 0, new_weights > 0)
    max_weight_delta = float(np.max(np.abs(old_weights-new_weights)))
    weights_bitwise_equal = np.array_equal(old_weights, new_weights)
    del old_weights, new_weights

    analyses = {name: [] for name in ("oracle", "cold", "warm")}
    for repetition in range(REPEATS):
        modes = ("oracle", "cold", "warm") if repetition % 2 == 0 else ("warm", "cold", "oracle")
        for mode in modes:
            area.clear_area_weight_cache()
            if mode == "warm":
                area._cell_area_sums(grid, mask, classes, geometry)
            @contextmanager
            def implementation():
                if mode == "oracle":
                    with patch.object(area, "_cell_area_sums", reference_cell_area_sums):
                        yield
                else:
                    yield
            with implementation():
                actual, sample = timed(lambda: analyze_local_akanda(*observations, DATA_DIR))
            scientific_check(actual)
            for field in ("ndvi_date_1", "ndvi_date_2", "valid_date_1", "valid_date_2",
                          "comparison_mask", "delta_ndvi"):
                np.testing.assert_array_equal(getattr(actual.comparison,field),getattr(result.comparison,field))
            assert actual.analysis.statistics == result.analysis.statistics
            assert actual.analysis.class_pixel_counts == result.analysis.class_pixel_counts
            assert actual.common_grid == result.common_grid
            analyses[mode].append(sample)
            print(f"analysis {repetition+1}/3 {mode}: {sample['seconds']:.6f}s", flush=True)
            del actual

    # Distinct AOIs, identical grid and masks; no source data or files required.
    synthetic_grid = RasterGrid(CRS.from_epsg(6933), (10.,10.),
                                Affine(10,0,0,0,-10,2560),256,256)
    synthetic_mask = np.random.default_rng(53).random((256,256)) > .3
    synthetic_classes = np.random.default_rng(57).integers(1,4,(256,256),dtype=np.uint8)
    synthetic_classes[~synthetic_mask] = 0
    aois = {
        "partial_rectangle": box(3.25,4.75,2551.5,2557.25),
        "hole": Polygon([(0,0),(2560,0),(2560,2560),(0,2560)],
                        holes=[[(703,611),(1801,611),(1801,1909),(703,1909)]]),
        "concave": Polygon([(1,1),(2559,1),(2559,701),(801,701),(801,2559),(1,2559)]),
        "multipart": MultiPolygon([box(1,1,1001,1101),box(1501,1401,2559,2559)]),
    }
    synthetic = {}
    for name, aoi in aois.items():
        samples = {mode: [] for mode in ("oracle","cold","warm")}
        expected = None
        for repetition in range(REPEATS):
            modes = ("oracle","cold","warm") if repetition % 2 == 0 else ("warm","cold","oracle")
            for mode in modes:
                area.clear_area_weight_cache()
                if mode == "warm":
                    area._cell_area_sums(synthetic_grid,synthetic_mask,synthetic_classes,aoi)
                function = reference_cell_area_sums if mode == "oracle" else area._cell_area_sums
                actual, sample = timed(lambda: function(synthetic_grid,synthetic_mask,synthetic_classes,aoi))
                if expected is None:
                    expected = actual
                assert_sums_equal(actual,expected)
                samples[mode].append(sample)
        synthetic[name] = {mode: summary(values) for mode,values in samples.items()}
        print(f"synthetic AOI {name}: complete", flush=True)

    report = {
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "environment": {"python": platform.python_version(),
                        "versions": {name:version(name) for name in
                                     ("numpy","rasterio","shapely","pyproj","geopandas")}},
        "protocol": {"repetitions":REPEATS,"alternating_order":True,
                     "cache_cold_definition":"area weight process cache cleared; OS file cache not flushed",
                     "rss":"10 ms process sampling; allocator retention and baseline included; peak may be missed",
                     "warm_analysis":"weights warm, source integrity and all other scientific steps rerun",
                     "saturated":"4 entries of 16 MiB; setup excluded from target timing"},
        "input": {"acquired_at":[o.acquired_at.isoformat() for o in observations],
                  "grid":[grid.width,grid.height],"crs":str(grid.crs),
                  "resolution":grid.resolution,"scl_policy":result.scl_policy,
                  "threshold":result.threshold,"aoi":"Akanda acquisition square, 12 km"},
        "akanda_surfaces":{mode:summary(samples) for mode,samples in surfaces.items()},
        "akanda_complete_analysis":{mode:summary(samples) for mode,samples in analyses.items()},
        "saturation_before":saturation_before,"saturation_after":saturation_after,
        "synthetic_256x256":synthetic,
        "science": {"digest":REFERENCE,"weights_bitwise_equal":weights_bitwise_equal,
                    "max_weight_delta_m2":max_weight_delta,"max_surface_delta_m2":max_surface_delta,
                    "max_coverage_delta_percentage_points":max_coverage_delta,
                    "statistics":result.analysis.statistics,"areas_m2":result.analysis.areas_m2,
                    "coverage_percent":result.analysis.comparable_coverage_percent},
        "limits":{"cache_bytes":area.AREA_CACHE_MAX_BYTES,"cache_entries":area.AREA_CACHE_MAX_ENTRIES,
                  "block_side":area.AREA_BLOCK_SIZE,"max_geometry_cells":area.MAX_AREA_CELLS_PER_CHUNK},
    }
    encoded = json.dumps(report,indent=2,ensure_ascii=True)
    if args.output is not None:
        args.output.parent.mkdir(parents=True,exist_ok=True)
        # Refuse to overwrite reports or any existing user file.
        with args.output.open('x',encoding='utf-8') as destination:
            destination.write(encoded+'\n')
    print(encoded)


if __name__ == "__main__":
    main()
