"""Manual diagnostic: per-component fingerprint of the 15→25/01/2025 pair on the demo pack.

Usage : python scripts/diagnose_pack_science.py <unpacked pack dir> [--json out.json]

Prints a SHA-256 per array (NDVI dates, ΔNDVI, masks, classes) and the exact
float values (``repr``) of statistics, areas and coverage, so two platforms can
be compared component by component. No network, no writes besides ``--json``.
"""

import argparse
import hashlib
import json
from pathlib import Path
import platform
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402

from src.geo.scientific_profile import GABON_EQUATORIAL_EXPERIMENTAL as PROFILE  # noqa: E402
from src.services.local_preview import analyze_local_akanda, list_local_observations  # noqa: E402


def _array(values: np.ndarray) -> dict:
    values = np.ascontiguousarray(values)
    return {"sha256": hashlib.sha256(values.tobytes()).hexdigest(), "dtype": str(values.dtype),
            "shape": list(values.shape)}


def fingerprint(pack: Path) -> dict:
    observations = {item.acquired_at.date().isoformat(): item for item in list_local_observations(pack)}
    result = analyze_local_akanda(observations["2025-01-15"], observations["2025-01-25"], pack,
                                  scientific_profile=PROFILE)
    comparison, analysis = result.comparison, result.analysis
    return {
        "platform": f"{platform.system()} Python {platform.python_version()}",
        "grid": [result.common_grid.width, result.common_grid.height, list(result.common_grid.transform)[:6]],
        "arrays": {name: _array(values) for name, values in {
            "ndvi_date_1": comparison.ndvi_date_1, "ndvi_date_2": comparison.ndvi_date_2,
            "valid_date_1": comparison.valid_date_1, "valid_date_2": comparison.valid_date_2,
            "comparison_mask": comparison.comparison_mask, "delta_ndvi": analysis.delta_ndvi,
            "comparable_mask": analysis.comparable_mask, "change_class_map": analysis.change_class_map,
        }.items()},
        "statistics": {key: repr(value) for key, value in analysis.statistics.items()},
        "areas_m2": {key: repr(value) for key, value in analysis.areas_m2.items()},
        "class_pixel_counts": dict(analysis.class_pixel_counts),
        "comparable_area_m2": repr(analysis.comparable_area_m2),
        "denominator_area_m2": repr(analysis.denominator_area_m2),
        "comparable_coverage_percent": repr(analysis.comparable_coverage_percent),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("pack", type=Path)
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()
    report = fingerprint(args.pack)
    text = json.dumps(report, indent=1, sort_keys=True)
    if args.json:
        args.json.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
