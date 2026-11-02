"""Record every value the Streamlit interface displays on the demo pack, for before/after comparisons.

Manual tool for the navigation reorganisation (docs/navigation.md). Each page and section is
replayed with ``AppTest`` on a copy of the unpacked v3 demo pack (files only, as installed from
the archive); on-demand computations are started by clicking their buttons. The output lists
the displayed elements per flow, in display order, with their exact values (tables and data
frames in full precision), and the cold duration of each flow.

    python scripts/ui_reference.py --layout e0 --pack <unpacked v3 pack> --output reference.json
    python scripts/ui_reference.py --compare reference.json other.json

Comparison: scientific elements (metrics, tables, data frames, JSON) must be identical as a
multiset over all flows; text elements are listed when they differ (titles and navigation are
expected to change, figures are not).
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("GEONAF_DEMO_PACK", "off")

SCIENTIFIC = ("metric", "table", "dataframe", "json")
TEXT = ("title", "header", "subheader", "markdown", "caption", "info", "warning", "error", "success", "text")
AOI = "akanda"

# Flow: (name, page to open, buttons to click in order when present). Pages are paths relative
# to the repository, opened through the entrypoint (app.py) like a visitor.
LAYOUTS = {
    "e0": [
        ("accueil", "app.py", (f"s2_vegetation_start_{AOI}", f"s2_pairs_start_{AOI}", f"s2_pairs_compute_{AOI}")),
        ("acquisitions", "views/acquisitions_sentinel2.py", ()),
        ("serie_ndvi", "views/serie_ndvi.py", ("series_compute",)),
        ("multisource", "views/analyses_multisources.py", ()),
        ("territoires", "views/territoires.py", ()),
    ],
}
TEMPORAL = "views/analyses_temporelles.py"
_SECTIONS = [
    ("t_comparer", f"{TEMPORAL}?section=comparer", (f"s2_pairs_start_{AOI}", f"s2_pairs_compute_{AOI}")),
    ("t_variation", f"{TEMPORAL}?section=variation", ()),
    ("t_serie", f"{TEMPORAL}?section=serie", ("series_compute",)),
    ("t_donnees", f"{TEMPORAL}?section=donnees", ()),
]
# E3: the home page is still complete; the temporal page holds the series and the catalogue.
LAYOUTS["e3"] = [LAYOUTS["e0"][0], *_SECTIONS, *LAYOUTS["e0"][3:]]
# V1: dashboard, temporal sections, multisource and territories pages.
LAYOUTS["v1"] = [("tableau_de_bord", "app.py", (f"s2_vegetation_start_{AOI}",)), *_SECTIONS,
                 ("multisource", "views/analyses_multisources.py", ()), ("territoires", "views/territoires.py", ())]
# Where each E0 flow is displayed now: (E0 flows, flows of the layout, relation).
GROUPS = {
    "e0": [((name,), (name,), "equal") for name, _, _ in LAYOUTS["e0"]],
    "e3": [(("accueil",), ("accueil",), "equal"), (("serie_ndvi",), ("t_serie",), "equal"),
           (("acquisitions",), ("t_donnees",), "equal"), (("multisource",), ("multisource",), "equal"),
           (("territoires",), ("territoires",), "equal"), (("accueil",), ("t_comparer", "t_variation"), "contains")],
    "v1": [(("accueil",), ("tableau_de_bord", "t_comparer", "t_variation"), "equal"),
           (("serie_ndvi",), ("t_serie",), "equal"), (("acquisitions",), ("t_donnees",), "equal"),
           (("multisource",), ("multisource",), "equal"), (("territoires",), ("territoires",), "equal")],
}


def copy_pack(pack: Path, target: Path) -> Path:
    for source in pack.rglob("*"):
        if source.is_file():
            destination = target / source.relative_to(pack)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
    return target


def _value(element) -> str:
    kind = element.type
    if kind == "metric":
        return json.dumps([element.label, element.value, element.delta, element.help], ensure_ascii=False)
    if kind in ("table", "dataframe"):
        return element.value.to_csv(lineterminator="\n")
    if kind == "json":
        return str(element.value)
    return str(element.value)


def elements(app) -> list[dict]:
    output = []
    for kind in (*SCIENTIFIC, *TEXT):
        for element in app.get(kind):
            output.append({"type": kind, "value": _value(element)})
    return output


def _clear_caches():
    import streamlit as st

    st.cache_data.clear()
    st.cache_resource.clear()
    from src.services import bounded_cache
    if bounded_cache._shared is not None:
        bounded_cache._shared.clear()


def run_flow(page: str, buttons, timeout: float):
    from streamlit.testing.v1 import AppTest

    app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=timeout).run()
    page, _, section = page.partition("?section=")
    if section:
        from src.navigation import SECTION_KEY, SECTIONS

        app.session_state[SECTION_KEY] = SECTIONS[section]
    if page != "app.py":
        app.switch_page(page).run()
    for key in buttons:
        matches = [button for button in app.button if button.key == key]
        if matches:
            matches[0].click().run()
    return app


def record(layout: str, pack: Path, timeout: float = 300) -> dict:
    import src.config
    from src.components import data_source
    import src.components.resource_panel as resource_panel

    resource_panel.available_memory_bytes = lambda: None   # Verdicts independent of host load.
    with tempfile.TemporaryDirectory() as folder:
        data_dir = copy_pack(pack, Path(folder) / pack.name)
        src.config.DATA_DIR = data_dir
        flows = {}
        for name, page, buttons in LAYOUTS[layout]:
            _clear_caches()
            data_source._resolve.clear()
            start = time.perf_counter()
            app = run_flow(page, buttons, timeout)
            seconds = time.perf_counter() - start
            flows[name] = {"page": page, "buttons": list(buttons), "cold_seconds": round(seconds, 2),
                           "exception": [str(e.value) for e in app.exception], "elements": elements(app)}
            print(f"{name}: {seconds:.1f} s, {len(flows[name]['elements'])} elements", file=sys.stderr)
    return {"layout": layout, "pack": pack.name, "flows": flows}


def _bag(document, kinds, flows=None):
    return Counter((item["type"], item["value"]) for name, flow in document["flows"].items()
                   if flows is None or name in flows for item in flow["elements"] if item["type"] in kinds)


def compare_groups(first: dict, second: dict) -> list:
    """Scientific elements of each E0 group, found again where the layout displays them."""
    output = []
    for before, after, relation in GROUPS[second["layout"]]:
        a, b = _bag(first, SCIENTIFIC, before), _bag(second, SCIENTIFIC, after)
        missing, extra = a - b, b - a
        ok = not missing and not extra if relation == "equal" else not extra   # "contains": after within before.
        output.append({"e0": list(before), "now": list(after), "relation": relation, "ok": ok,
                       "e0_count": sum(a.values()), "now_count": sum(b.values()),
                       "missing": sorted(missing.elements()) if relation == "equal" else [],
                       "extra": sorted(extra.elements())})
    return output


def compare(first: dict, second: dict) -> dict:
    bag = _bag
    report = {}
    for label, kinds in (("scientific", SCIENTIFIC), ("text", TEXT)):
        a, b = bag(first, kinds), bag(second, kinds)
        report[label] = {"only_first": sorted((a - b).elements()), "only_second": sorted((b - a).elements()),
                         "first_count": sum(a.values()), "second_count": sum(b.values())}
    if second["layout"] in GROUPS:
        report["groups"] = compare_groups(first, second)
    report["exceptions"] = {name: flow["exception"] for document in (first, second)
                            for name, flow in document["flows"].items() if flow["exception"]}
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--layout", choices=sorted(LAYOUTS))
    parser.add_argument("--pack", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--compare", nargs=2, type=Path)
    arguments = parser.parse_args()
    if arguments.compare:
        documents = [json.loads(path.read_text(encoding="utf-8")) for path in arguments.compare]
        sys.stdout.reconfigure(encoding="utf-8")
        print(json.dumps(compare(*documents), ensure_ascii=False, indent=1))
        return
    document = record(arguments.layout, arguments.pack)
    arguments.output.write_text(json.dumps(document, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
