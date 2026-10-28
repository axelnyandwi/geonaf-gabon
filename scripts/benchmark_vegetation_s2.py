"""Time and memory of the Sentinel-2 vegetation inventory (manual diagnostic, no network).

Usage : python scripts/benchmark_vegetation_s2.py [--output docs/benchmarks/vegetation_s2.json]

Each case runs in a fresh Python process:

* cold   — first inventory of the process (exact area weights computed, files read from disk);
* warm   — same inventory again in the same process (area-weight cache hit, OS file cache warm);
* traced — third run under tracemalloc, for the peak of Python allocations (numpy included);
  tracemalloc slows the run, so its time is not reported.

Process memory: resident set size after imports, after the runs, and the process peak
(Windows ``peak_wset``; ``ru_maxrss`` elsewhere). The size of the pickled inventory is what a
Streamlit cache would retain.
"""

from __future__ import annotations

import argparse
import json
import pickle
import platform
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CASES = (("paquet v3", "data/demo_pack/geonaf-demo-akanda-2025-01-v3", "akanda"),
         ("données locales", "data", "akanda"), ("données locales", "data", "libreville"),
         ("données locales", "data", "owendo"))


def _peak_rss():
    import psutil

    info = psutil.Process().memory_info()
    if hasattr(info, "peak_wset"):
        return info.peak_wset
    import resource

    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024


def run_case(data_dir: str, aoi_id: str) -> dict:
    from time import perf_counter
    import tracemalloc

    import psutil

    sys.path.insert(0, str(ROOT))
    from src.indicators.s2_inventory import s2_vegetation_inventory

    process, mib = psutil.Process(), 1024 * 1024
    baseline = process.memory_info().rss
    run = lambda: s2_vegetation_inventory(ROOT / data_dir, aoi_id, available_bytes=None)
    start = perf_counter()
    inventory = run()
    cold = perf_counter() - start
    start = perf_counter()
    run()
    warm = perf_counter() - start
    tracemalloc.start()
    run()
    traced_peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    return {
        "acquisitions": len(inventory.estimates), "failures": len(inventory.failures),
        "published": sum(e.assessment.available for e in inventory.estimates),
        "cold_s": round(cold, 2), "warm_s": round(warm, 2),
        "python_peak_mib": round(traced_peak / mib, 1),
        "rss_after_imports_mib": round(baseline / mib, 1), "rss_after_runs_mib": round(process.memory_info().rss / mib, 1),
        "process_peak_mib": round(_peak_rss() / mib, 1),
        "cached_inventory_bytes": len(pickle.dumps(inventory)),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output")
    parser.add_argument("--case", nargs=2, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.case:
        print(json.dumps(run_case(*args.case)))
        return
    results = []
    for label, data_dir, aoi_id in CASES:
        if not (ROOT / data_dir / "aoi" / f"{aoi_id}.geojson").is_file():
            print(f"{label} / {aoi_id} : zone absente, ignorée")
            continue
        output = subprocess.run([sys.executable, __file__, "--case", data_dir, aoi_id], capture_output=True,
                                text=True, check=True, cwd=ROOT)
        measure = json.loads(output.stdout.strip().splitlines()[-1])
        results.append({"source": label, "zone": aoi_id, **measure})
        print(f"{label:<16} {aoi_id:<11} {measure['acquisitions']} acq. ({measure['published']} publiées) · "
              f"froid {measure['cold_s']:5.2f} s · chaud {measure['warm_s']:5.2f} s · pic Python "
              f"{measure['python_peak_mib']:6.1f} Mio · pic processus {measure['process_peak_mib']:6.1f} Mio "
              f"(après imports {measure['rss_after_imports_mib']:6.1f}) · cache {measure['cached_inventory_bytes']} o")
    if args.output:
        import numpy, rasterio

        Path(args.output).write_text(json.dumps({
            "date_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "platform": platform.platform(), "python": platform.python_version(),
            "numpy": numpy.__version__, "rasterio": rasterio.__version__, "results": results,
        }, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
