"""Time and memory of the Sentinel-2 pair selection and transitions (manual diagnostic, no network).

Usage : python scripts/benchmark_transitions_s2.py [--output docs/benchmarks/transitions_s2.json]

Two fresh Python processes per zone, so that each process peak belongs to one step:

* inventory — the L3 inventory (``s2_vegetation_inventory``), pickled to a temporary file;
* pairs     — loads that inventory, then measures separately:
  - selection: ``plan_pairs`` (pairs, upper bounds, recency order; no file read);
  - computation cold / warm: ``s2_pair_selection`` with the given inventory (resource guard, SHA-256 of the
    assets, historical pair chain, transitions); warm = same call again (area-weight cache hit, OS file cache);
  - traced: third call under tracemalloc (Python allocations, numpy included; time not reported).

Process memory: resident set size after imports and after the runs, and the process peak (Windows
``peak_wset``; ``ru_maxrss`` elsewhere), which also counts GDAL buffers that tracemalloc does not see. The
size of the pickled selection is what a Streamlit cache would retain.
"""

from __future__ import annotations

import argparse
import json
import pickle
import platform
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CASES = (("paquet v3", "data/demo_pack/geonaf-demo-akanda-2025-01-v3", "akanda"),
         ("données locales", "data", "akanda"), ("données locales", "data", "libreville"),
         ("données locales", "data", "owendo"))
MIB = 1024 * 1024


def _peak_rss():
    import psutil

    info = psutil.Process().memory_info()
    if hasattr(info, "peak_wset"):
        return info.peak_wset
    import resource

    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024


def run_inventory(data_dir: str, aoi_id: str, target: str) -> dict:
    from time import perf_counter

    import psutil

    sys.path.insert(0, str(ROOT))
    from src.indicators.s2_inventory import s2_vegetation_inventory

    baseline = psutil.Process().memory_info().rss
    start = perf_counter()
    inventory = s2_vegetation_inventory(ROOT / data_dir, aoi_id, available_bytes=None)
    elapsed = perf_counter() - start
    Path(target).write_bytes(pickle.dumps(inventory))
    return {"inventory_s": round(elapsed, 2), "acquisitions": len(inventory.estimates),
            "inventory_rss_after_imports_mib": round(baseline / MIB, 1),
            "inventory_process_peak_mib": round(_peak_rss() / MIB, 1)}


def run_pairs(data_dir: str, aoi_id: str, source: str) -> dict:
    from time import perf_counter
    import tracemalloc

    import psutil

    sys.path.insert(0, str(ROOT))
    from src.indicators.s2_pairs import PUBLISHABLE, REJECTED, plan_pairs, s2_pair_selection

    process = psutil.Process()
    inventory = pickle.loads(Path(source).read_bytes())
    baseline = process.memory_info().rss
    start = perf_counter()
    candidates, rejected = plan_pairs(inventory)
    selection_s = perf_counter() - start
    run = lambda: s2_pair_selection(ROOT / data_dir, aoi_id, inventory=inventory, available_bytes=None)
    start = perf_counter()
    result = run()
    cold = perf_counter() - start
    start = perf_counter()
    run()
    warm = perf_counter() - start
    tracemalloc.start()
    run()
    traced_peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    default = result.default_pair()
    return {
        "pairs": len(result.pairs), "candidates": len(candidates), "rejected_before_computation": len(rejected),
        "computed": sum(p.measure is not None or p.assessment is not None for p in result.pairs),
        "default": None if default is None else default.label,
        "published": sum(p.status == PUBLISHABLE for p in result.pairs),
        "rejected": sum(p.status == REJECTED for p in result.pairs),
        "selection_ms": round(1000 * selection_s, 3), "computation_cold_s": round(cold, 2),
        "computation_warm_s": round(warm, 2), "python_peak_mib": round(traced_peak / MIB, 1),
        "rss_after_imports_mib": round(baseline / MIB, 1), "rss_after_runs_mib": round(process.memory_info().rss / MIB, 1),
        "process_peak_mib": round(_peak_rss() / MIB, 1), "cached_selection_bytes": len(pickle.dumps(result)),
    }


def _child(*args):
    output = subprocess.run([sys.executable, __file__, *args], capture_output=True, text=True, check=True, cwd=ROOT,
                            encoding="utf-8")
    return json.loads(output.stdout.strip().splitlines()[-1])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output")
    parser.add_argument("--inventory", nargs=3, help=argparse.SUPPRESS)
    parser.add_argument("--pairs", nargs=3, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.inventory or args.pairs:
        result = run_inventory(*args.inventory) if args.inventory else run_pairs(*args.pairs)
        print(json.dumps(result, ensure_ascii=False))
        return
    results = []
    with tempfile.TemporaryDirectory() as folder:
        for label, data_dir, aoi_id in CASES:
            if not (ROOT / data_dir / "aoi" / f"{aoi_id}.geojson").is_file():
                print(f"{label} / {aoi_id} : zone absente, ignorée")
                continue
            stored = str(Path(folder) / f"{aoi_id}-{len(results)}.pickle")
            measure = {**_child("--inventory", data_dir, aoi_id, stored), **_child("--pairs", data_dir, aoi_id, stored)}
            results.append({"source": label, "zone": aoi_id, **measure})
            print(f"{label:<16} {aoi_id:<11} {measure['acquisitions']} acq., {measure['pairs']} paires "
                  f"({measure['computed']} calculées, défaut {measure['default']}) · inventaire "
                  f"{measure['inventory_s']:5.2f} s · sélection {measure['selection_ms']:.3f} ms · calcul froid "
                  f"{measure['computation_cold_s']:5.2f} s / chaud {measure['computation_warm_s']:5.2f} s · pic "
                  f"Python {measure['python_peak_mib']:6.1f} Mio · pic processus {measure['process_peak_mib']:6.1f} "
                  f"Mio (après imports {measure['rss_after_imports_mib']:6.1f}) · cache "
                  f"{measure['cached_selection_bytes']} o")
    if args.output:
        import numpy, rasterio

        Path(args.output).write_text(json.dumps({
            "date_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "platform": platform.platform(), "python": platform.python_version(),
            "numpy": numpy.__version__, "rasterio": rasterio.__version__, "results": results,
        }, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
