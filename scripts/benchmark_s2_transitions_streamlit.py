"""Home page timings around the Sentinel-2 temporal comparison with AppTest (manual diagnostic, no network).

Usage : python scripts/benchmark_s2_transitions_streamlit.py [data_dir ...] [--output file.json]
        (default: demo pack v3)

Each mode runs in a fresh process so that every cache starts cold:

* before — home page with the comparison sub-section replaced by a no-op (state before L4c);
* after  — real home page: initial load, explicit start (first pair computation, L3 inventory
  included since it is not computed yet), change of pair (rerun), then a second session in the
  same process starting the comparison (cache shared by sessions).

Process peak memory: Windows ``peak_wset`` (``ru_maxrss`` elsewhere), for the whole process.
AppTest runs the script in-process without a browser: timings include the whole script (key
figures, scientific pair analysis) but no network or rendering.
"""

import json
import platform
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
DEFAULT = ROOT / "data" / "demo_pack" / "geonaf-demo-akanda-2025-01-v3"
START, PAIR = "s2_pairs_start_akanda", "s2_pairs_pair_akanda"
MIB = 1024 * 1024


def _peak_rss():
    import psutil

    info = psutil.Process().memory_info()
    if hasattr(info, "peak_wset"):
        return info.peak_wset
    import resource

    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024


def measure(mode: str, data_dir: Path) -> dict:
    sys.path.insert(0, str(ROOT))
    import src.config
    import src.components.s2_transitions as section
    from streamlit.testing.v1 import AppTest

    src.config.DATA_DIR = data_dir
    if mode == "before":
        section.show_s2_transitions = lambda *args, **kwargs: None
    timings = {}
    start = perf_counter()
    app = AppTest.from_file(str(ROOT / "app.py")).run(timeout=300)
    timings["chargement_initial_s"] = perf_counter() - start
    if mode == "after":
        start = perf_counter()
        app.button(key=START).click().run(timeout=300)
        timings["premier_calcul_paire_s"] = perf_counter() - start
        selector = app.selectbox(key=PAIR)
        other = next((i for i in range(len(selector.options)) if i != selector.value), None)
        if other is not None:
            start = perf_counter()
            app.selectbox(key=PAIR).set_value(other).run(timeout=300)
            timings["changement_de_paire_s"] = perf_counter() - start
            timings["paire_choisie"] = selector.format_func(other)
        second = AppTest.from_file(str(ROOT / "app.py")).run(timeout=300)
        start = perf_counter()
        second.button(key=START).click().run(timeout=300)
        timings["autre_session_cache_partage_s"] = perf_counter() - start
        timings["erreurs"] = [str(e.value) for e in app.exception] + [str(e.value) for e in second.exception]
    timings["pic_processus_mib"] = _peak_rss() / MIB
    return {key: round(value, 2) if isinstance(value, float) else value for key, value in timings.items()}


def main():
    if len(sys.argv) > 2 and sys.argv[1] == "--mode":
        print(json.dumps(measure(sys.argv[2], Path(sys.argv[3])), ensure_ascii=False))
        return
    args = sys.argv[1:]
    output = None
    if "--output" in args:
        output = args[args.index("--output") + 1]
        args = args[:args.index("--output")]
    results = []
    for data_dir in [Path(a) for a in args] or [DEFAULT]:
        for mode in ("before", "after"):
            run = subprocess.run([sys.executable, __file__, "--mode", mode, str(data_dir)], capture_output=True,
                                 text=True, check=True, cwd=ROOT, encoding="utf-8")
            measure_ = json.loads(run.stdout.strip().splitlines()[-1])
            source = "paquet v3" if data_dir.resolve() == DEFAULT.resolve() else "données locales"
            results.append({"source": source, "mode": mode, **measure_})
            print(source, mode, json.dumps(measure_, ensure_ascii=False))
    if output:
        import streamlit

        Path(output).write_text(json.dumps({
            "date_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"), "platform": platform.platform(),
            "python": platform.python_version(), "streamlit": streamlit.__version__, "results": results,
        }, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
