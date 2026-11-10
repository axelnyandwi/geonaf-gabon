"""Manual diagnostic: per-component fingerprint of a Sentinel-2 pair on a demo pack (v3, or v4 with --estuaire).

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


ESTUAIRE_PAIRS = {"akanda": (("2025-01-15", "2025-01-25"), ("2021-01-31", "2025-01-25")),
                  "libreville": (("2025-01-15", "2025-01-25"), ("2021-01-31", "2025-01-25")),
                  "owendo": (("2021-01-31", "2025-01-25"),)}  # No usable 2025 pair over Owendo (0,12 %).


def fingerprint(pack: Path, aoi_id: str | None = None, pair=("2025-01-15", "2025-01-25")) -> dict:
    """Without ``aoi_id``: the v3 Akanda pair (unchanged). With it: that territory's own subsets and profile."""
    from src.geo.scientific_profile import reference_profile

    profile = PROFILE if aoi_id is None else reference_profile(aoi_id)
    observations = {item.acquired_at.date().isoformat(): item for item in list_local_observations(pack, aoi_id)}
    result = analyze_local_akanda(observations[pair[0]], observations[pair[1]], pack, scientific_profile=profile)
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


def estuaire_fingerprints(pack: Path) -> dict:
    """Estuaire v4 pack: one fingerprint per territory and pair (``ESTUAIRE_PAIRS``)."""
    report = {"platform": f"{platform.system()} Python {platform.python_version()}", "territories": {}}
    for aoi_id, pairs in ESTUAIRE_PAIRS.items():
        report["territories"][aoi_id] = {}
        for pair in pairs:
            current = fingerprint(pack, aoi_id, pair)
            current.pop("platform")
            report["territories"][aoi_id][f"{pair[0]}_{pair[1]}"] = current
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("pack", type=Path)
    parser.add_argument("--json", type=Path)
    parser.add_argument("--estuaire", action="store_true", help="Paquet Estuaire v4 : trois territoires.")
    args = parser.parse_args()
    report = estuaire_fingerprints(args.pack) if args.estuaire else fingerprint(args.pack)
    text = json.dumps(report, indent=1, sort_keys=True)
    if args.json:
        args.json.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
