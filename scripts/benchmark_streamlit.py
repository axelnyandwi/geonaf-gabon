"""Manual offline profile of real Akanda inputs. Run from the repository root.

Timings are exclusive (nested measured phases are subtracted). Streamlit
timings cover Python serialization, not browser paint. No source data writes.
"""

from contextlib import contextmanager, ExitStack
from contextvars import ContextVar
import argparse
import hashlib
import json
from pathlib import Path
import sys
from time import perf_counter
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import rasterio
from streamlit.testing.v1 import AppTest


def scientific_digest(result):
    digest = hashlib.sha256()
    for values in (result.comparison.ndvi_date_1, result.comparison.ndvi_date_2,
                   result.analysis.delta_ndvi, result.analysis.comparable_mask,
                   result.analysis.change_class_map):
        digest.update(np.ascontiguousarray(values).tobytes())
    digest.update(json.dumps({"stats": result.analysis.statistics,
                              "areas": result.analysis.areas_m2,
                              "coverage": result.analysis.comparable_coverage_percent},
                             sort_keys=True).encode())
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-digest", help="Scientific SHA-256 from a previous run")
    expected = parser.parse_args().expected_digest
    totals = {}
    stack = ContextVar("timing_stack", default=())

    @contextmanager
    def phase(name):
        frame = [perf_counter(), 0.0]
        parents = stack.get()
        token = stack.set((*parents, frame))
        try:
            yield
        finally:
            elapsed = perf_counter() - frame[0]
            totals[name] = totals.get(name, 0.0) + elapsed - frame[1]
            if parents:
                parents[-1][1] += elapsed
            stack.reset(token)

    def wrap(function, name):
        def measured(*args, **kwargs):
            with phase(name):
                return function(*args, **kwargs)
        return measured

    original_open = rasterio.open

    class Reader:
        def __init__(self, source):
            self.source = source

        def __getattr__(self, name):
            return getattr(self.source, name)

        def __enter__(self):
            self.source.__enter__()
            return self

        def __exit__(self, *args):
            return self.source.__exit__(*args)

        def read(self, *args, **kwargs):
            name = "jp2_read" if str(self.source.name).lower().endswith(".jp2") else "other_raster_read"
            with phase(name):
                return self.source.read(*args, **kwargs)

    def open_raster(*args, **kwargs):
        source = original_open(*args, **kwargs)
        return Reader(source) if source.mode == "r" else source

    targets = {
        "src.services.local_preview._load_observation": "source_verification",
        "src.services.temporal_analysis.calculate_ndvi": "boa_ndvi",
        "src.geo.temporal.scl_policy_mask_on_grid": "scl_masks",
        "src.services.temporal_analysis.harmonize_ndvi_pair": "temporal_grid",
        "src.services.temporal_analysis.analyze_ndvi_changes": "temporal_statistics",
        "src.components.ndvi_maps.ndvi_maps": "map_generation",
        "streamlit.pyplot": "streamlit_render",
        "streamlit.image": "streamlit_render",
    }
    import importlib

    try:
        importlib.import_module("src.components.preview_cache")
    except ModuleNotFoundError:
        pass  # Baseline before the image cache exists.
    else:
        targets["src.components.preview_cache.render_ndvi_png"] = "map_encoding"

    with ExitStack() as patches:
        patches.enter_context(patch("rasterio.open", open_raster))
        for target, name in targets.items():
            module, attribute = target.rsplit(".", 1)
            original = getattr(importlib.import_module(module), attribute)
            patches.enter_context(patch(target, wrap(original, name)))
        app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py"))
        reports = []
        for mode in ("first_load", "same_pair_submit", "idle_rerun"):
            totals.clear()
            start = perf_counter()
            if mode == "same_pair_submit":
                app.button[0].click()
            app.run(timeout=180)
            elapsed = perf_counter() - start
            if app.exception or app.error:
                raise RuntimeError("Application profile failed; no private error details emitted.")
            reports.append({"mode": mode, "seconds": elapsed,
                            "phases": dict(totals),
                            "scientific_digest": scientific_digest(app.session_state["result"])})
        digests = {report["scientific_digest"] for report in reports}
        if len(digests) != 1 or (expected is not None and digests != {expected}):
            raise RuntimeError("Scientific arrays or indicators differ from the reference.")
        print(json.dumps(reports, indent=2, ensure_ascii=True))


if __name__ == "__main__":
    main()
