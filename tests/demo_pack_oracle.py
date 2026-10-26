"""Full-tile vs demo-pack comparison, shared by the regression test and the report script.

Runs every scientific entry point twice — on the verified full Sentinel-2 tiles
in ``data/`` and on the subsets of the demo pack — and compares the results
field by field. Only fields that legitimately describe the input files are
skipped (paths, input raster dimensions, computation timestamps).

Tolerance policy:
- arrays, masks, integers, strings, dates, grids, PNG/JSON payloads: exact;
- float scalars: exact expected (same pixels, same operations, same order);
  a difference is still classified against ``FLOAT_RTOL``/``FLOAT_ATOL`` so a
  report can tell a float64 rounding artefact from a real divergence.

Usage : python -m tests.demo_pack_oracle [--report docs/benchmarks/demo_pack_validation.json]
"""

from __future__ import annotations

from dataclasses import fields, is_dataclass
from datetime import date, datetime
from enum import Enum
import json
from math import isclose, isnan
from pathlib import Path
from typing import Mapping

import numpy as np

from src.config import DATA_DIR

PACK_DIR = DATA_DIR / "demo_pack" / "geonaf-demo-akanda-2025-01-v2"
AOI_ID = "akanda"
PRODUCTS = (
    "S2B_MSIL2A_20250115T093239_N0511_R136_T32NNF_20250115T120528",
    "S2A_MSIL2A_20250120T093321_N0511_R136_T32NNF_20250120T125148",
    "S2B_MSIL2A_20250125T093159_N0511_R136_T32NNF_20250125T115658",
)
PAIRS = ((0, 2), (0, 1), (1, 2))
FLOAT_RTOL = 1e-12
FLOAT_ATOL = 1e-12
# Fields describing input files, not scientific results.
SKIPPED_FIELDS = frozenset({"asset_paths", "asset_metadata", "path", "aoi_path", "calibration_xml",
                            "computed_at_utc", "source_sha256"})


def available() -> bool:
    return (PACK_DIR / "PACK_MANIFEST.json").is_file() and all(
        (DATA_DIR / "raw" / "sentinel2" / product / "acquisition_manifest.json").is_file() for product in PRODUCTS)


def _acquisition(directory: Path, manifest_name: str):
    from src.data.acquisition import create_sentinel2_acquisition

    manifest = json.loads((directory / manifest_name).read_text(encoding="utf-8"))
    paths = {role: directory / record["path"] for role, record in manifest["assets"].items()}
    return create_sentinel2_acquisition(
        product_id=manifest["product_id"],
        acquired_at=datetime.fromisoformat(manifest["acquired_at_utc"].replace("Z", "+00:00")),
        b04_path=paths["b04"], b08_path=paths["b08"], scl_path=paths["scl"],
        calibration_xml_path=paths["calibration_xml"],
        asset_product_ids={role: record["product_id"] for role, record in manifest["assets"].items()})


def load_sources() -> dict:
    """Full-tile and subset acquisitions with the AOI of each side (byte-identical files)."""
    full_root, pack_root = DATA_DIR / "raw" / "sentinel2", PACK_DIR / "raw" / "sentinel2"
    return {
        "full": ([_acquisition(full_root / p, "acquisition_manifest.json") for p in PRODUCTS],
                 DATA_DIR / "aoi" / f"{AOI_ID}.geojson", DATA_DIR),
        "subset": ([_acquisition(pack_root / p, "subset_manifest.json") for p in PRODUCTS],
                   PACK_DIR / "aoi" / f"{AOI_ID}.geojson", PACK_DIR),
    }


def compare(left, right, path: str = "") -> list[dict]:
    """Recursive comparison; returns the mismatches (empty list when identical)."""
    if is_dataclass(left) and not isinstance(left, type):
        if type(left) is not type(right):
            return [{"path": path, "kind": "type"}]
        output = []
        for item in fields(left):
            if item.name not in SKIPPED_FIELDS:
                output += compare(getattr(left, item.name), getattr(right, item.name), f"{path}.{item.name}")
        return output
    if isinstance(left, Mapping):
        if not isinstance(right, Mapping) or set(left) != set(right):
            return [{"path": path, "kind": "keys"}]
        output = []
        for key in left:
            if key not in SKIPPED_FIELDS:
                output += compare(left[key], right[key], f"{path}[{key!r}]")
        return output
    if isinstance(left, np.ndarray):
        if not isinstance(right, np.ndarray) or left.shape != right.shape or left.dtype != right.dtype:
            return [{"path": path, "kind": "array_shape_or_dtype"}]
        if np.array_equal(left, right, equal_nan=left.dtype.kind in "fc"):
            return []
        mismatch = {"path": path, "kind": "array", "differing_cells": int(np.count_nonzero(left != right))}
        if left.dtype.kind == "f":
            mismatch["max_abs_diff"] = float(np.nanmax(np.abs(left.astype(np.float64) - right)))
        return [mismatch]
    if isinstance(left, (list, tuple)):  # Includes Affine (a namedtuple): coefficients compared exactly.
        if not isinstance(right, (list, tuple)) or len(left) != len(right):
            return [{"path": path, "kind": "length"}]
        output = []
        for index, (a, b) in enumerate(zip(left, right)):
            output += compare(a, b, f"{path}[{index}]")
        return output
    if isinstance(left, (float, np.floating)) and not isinstance(left, bool):
        a, b = float(left), float(right)
        if a == b or (isnan(a) and isnan(b)):
            return []
        return [{"path": path, "kind": "float", "left": a, "right": b, "abs_diff": abs(a - b),
                 "within_tolerance": isclose(a, b, rel_tol=FLOAT_RTOL, abs_tol=FLOAT_ATOL)}]
    if isinstance(left, (str, bytes, int, bool, type(None), datetime, date, Enum)) or hasattr(left, "to_wkt") \
            or type(left).__name__ == "Affine":
        return [] if left == right else [{"path": path, "kind": "value", "left": str(left)[:120],
                                          "right": str(right)[:120]}]
    return [] if left == right else [{"path": path, "kind": "other", "type": type(left).__name__}]


def raster_checks(sources) -> dict:
    """Subset pixels vs the same window read from the full tile, then AOI clips of both."""
    import rasterio
    from rasterio.windows import from_bounds
    from tempfile import TemporaryDirectory
    from src.data.preparation import clip_raster_to_aoi

    report = {}
    (full, full_aoi, _), (subset, subset_aoi, _) = sources["full"], sources["subset"]
    with TemporaryDirectory(prefix="geonaf-demo-pack-") as scratch:
        for a, b in zip(full, subset):
            for role in ("b04", "b08", "scl"):
                full_path, subset_path = getattr(a, role).path, getattr(b, role).path
                with rasterio.open(subset_path) as small, rasterio.open(full_path) as large:
                    window = from_bounds(*small.bounds, transform=large.transform)
                    window_equal = bool(np.array_equal(small.read(1), large.read(1, window=window.round_offsets()
                                                                                   .round_lengths())))
                clipped = []
                for label, source, aoi in (("full", full_path, full_aoi), ("subset", subset_path, subset_aoi)):
                    target = Path(scratch) / f"{a.product_id}-{role}-{label}.tif"
                    clip_raster_to_aoi(source, target, aoi, all_touched=True)
                    with rasterio.open(target) as dataset:
                        clipped.append((dataset.read(), dataset.transform, dataset.crs, dataset.nodata))
                report[f"{a.product_id}/{role}"] = {
                    "subset_equals_tile_window": window_equal,
                    "aoi_clip_pixels_equal": bool(np.array_equal(clipped[0][0], clipped[1][0])),
                    "aoi_clip_grid_equal": clipped[0][1:] == clipped[1][1:],
                    "aoi_clip_shape": list(clipped[0][0].shape)}
    return report


def scientific_results(acquisitions, aoi_path, data_dir) -> dict:
    from src.geo.scientific_profile import reference_profile
    from src.services.ndvi_observation import analyze_ndvi_observation
    from src.services.ndvi_time_series import analyze_ndvi_time_series
    from src.services.temporal_analysis import analyze_temporal_vegetation_change
    from src.services.temporal_map import prepare_temporal_map
    from src.services.territorial_indicators import compute_territorial_indicators, indicator_inputs

    profile = reference_profile(AOI_ID)
    results = {f"observation/{a.product_id[11:19]}": analyze_ndvi_observation(
        a, aoi_path=aoi_path, scientific_profile=profile) for a in acquisitions}
    for first, second in PAIRS:
        name = f"pair/{acquisitions[first].product_id[11:19]}-{acquisitions[second].product_id[11:19]}"
        result = analyze_temporal_vegetation_change(acquisitions[first], acquisitions[second],
                                                    aoi_path=aoi_path, scientific_profile=profile)
        results[name] = result
        results[f"{name}/map_payload"] = prepare_temporal_map(result, max_size=1536)
    results["series/3_dates"] = analyze_ndvi_time_series(acquisitions, aoi_path=aoi_path,
                                                         scientific_profile=profile)
    inputs = indicator_inputs(data_dir, AOI_ID)
    results["territorial_indicators"] = compute_territorial_indicators(inputs, aoi_label="Akanda")
    return results


def _headline(name, result) -> dict:
    """Readable figures of each result (taken from the subset run, equal on both sides)."""
    if name.startswith("observation/"):
        return {"valid_coverage_percent": result.valid_coverage_percent,
                "ndvi_mean": result.summary.mean, "valid_pixels": result.summary.count}
    if name.startswith("pair/") and not name.endswith("map_payload"):
        analysis = result.analysis
        return {"comparable_coverage_percent": analysis.comparable_coverage_percent,
                "delta_mean": analysis.statistics["mean"], "areas_ha": dict(analysis.areas_ha),
                "status": result.interpretation.status.value}
    if name.startswith("series/"):
        return {"dates": [{"product": d.product_id[11:19], "coverage": d.valid_coverage_percent,
                           "included": d.included, "reason": d.exclusion_reason} for d in result.dates],
                "common_coverage_percent": result.common_coverage_percent}
    return {}


def run() -> dict:
    sources = load_sources()
    rasters = raster_checks(sources)
    full = scientific_results(*sources["full"])
    subset = scientific_results(*sources["subset"])
    comparisons = {}
    for name in full:
        mismatches = compare(full[name], subset[name], name)
        comparisons[name] = {"identical": not mismatches, "mismatches": mismatches,
                             "headline": _headline(name, subset[name])}
    return {"rasters": rasters, "results": comparisons,
            "tolerance": {"arrays": "exact", "float_scalars": "exact expected",
                          "float_rtol": FLOAT_RTOL, "float_atol": FLOAT_ATOL}}


if __name__ == "__main__":
    import argparse
    import sys

    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if not available():
        sys.exit("Données complètes ou paquet de démonstration absents.")
    report = run()
    text = json.dumps(report, ensure_ascii=False, indent=2, default=str)
    if args.report:
        args.report.write_text(text + "\n", encoding="utf-8")
    raster_ok = all(all(value for key, value in item.items() if key != "aoi_clip_shape")
                    for item in report["rasters"].values())
    print(f"Rasters : {len(report['rasters'])} contrôles, tous identiques = {raster_ok}")
    for name, item in report["results"].items():
        print(f"{'IDENTIQUE' if item['identical'] else 'DIFFÉRENT'}  {name}  {len(item['mismatches'])} écart(s)")
