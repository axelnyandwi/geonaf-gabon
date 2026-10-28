"""Home page timings around the Sentinel-2 vegetation section with AppTest (manual diagnostic, no network).

Usage : python scripts/benchmark_s2_vegetation_streamlit.py [data_dir]   (default: demo pack v3)

Each mode runs in a fresh process so that every cache starts cold:

* before — home page with the Sentinel-2 section replaced by a no-op (state before L3c);
* after  — real home page: initial load, explicit start (first Sentinel-2 computation),
  change of acquisition (rerun on the cached aggregate), then a second session in the
  same process starting the estimate (cache shared by sessions).

AppTest runs the script in-process without a browser: timings include the whole script
(key figures, pair analysis) but no network or rendering.
"""

import json
import subprocess
import sys
from pathlib import Path
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
DEFAULT = ROOT / "data" / "demo_pack" / "geonaf-demo-akanda-2025-01-v3"


def measure(mode: str, data_dir: Path) -> dict:
    sys.path.insert(0, str(ROOT))
    import src.config
    import src.components.s2_vegetation as section
    from streamlit.testing.v1 import AppTest

    src.config.DATA_DIR = data_dir
    if mode == "before":
        section.show_s2_vegetation = lambda *args, **kwargs: None
    timings = {}
    start = perf_counter()
    app = AppTest.from_file(str(ROOT / "app.py")).run(timeout=300)
    timings["chargement_initial_s"] = perf_counter() - start
    if mode == "after":
        start = perf_counter()
        app.button(key="s2_vegetation_start_akanda").click().run(timeout=300)
        timings["premier_calcul_s2_s"] = perf_counter() - start
        start = perf_counter()
        app.selectbox(key="s2_vegetation_date_akanda").set_value(1).run(timeout=300)
        timings["changement_de_date_s"] = perf_counter() - start
        second = AppTest.from_file(str(ROOT / "app.py")).run(timeout=300)
        start = perf_counter()
        second.button(key="s2_vegetation_start_akanda").click().run(timeout=300)
        timings["autre_session_cache_partage_s"] = perf_counter() - start
        timings["erreurs"] = [str(e.value) for e in app.exception]
    return {key: round(value, 2) if isinstance(value, float) else value for key, value in timings.items()}


def main():
    if len(sys.argv) > 2 and sys.argv[1] == "--mode":
        print(json.dumps(measure(sys.argv[2], Path(sys.argv[3]))))
        return
    data_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT
    for mode in ("before", "after"):
        output = subprocess.run([sys.executable, __file__, "--mode", mode, str(data_dir)], capture_output=True,
                                text=True, check=True, cwd=ROOT)
        print(mode, output.stdout.strip().splitlines()[-1])


if __name__ == "__main__":
    main()
