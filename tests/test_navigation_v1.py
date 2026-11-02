"""Navigation V1: router pages, temporal analysis sections, shared study zone (docs/navigation.md)."""

from pathlib import Path
from unittest.mock import Mock

import pytest
from streamlit.testing.v1 import AppTest

from src import navigation

from tests.test_local_preview import local_data  # noqa: F401  (fixture)

ROOT = Path(__file__).resolve().parents[1]
TEMPORAL = "views/analyses_temporelles.py"
LABELS = navigation.SECTIONS
SECTIONS = {"comparer": ("src.components.s2_transitions", "show_s2_transitions"),
            "variation": ("src.components.pair_analysis", "show_pair_analysis"),
            "serie": ("src.components.ndvi_series", "show_ndvi_series"),
            "donnees": ("src.components.acquisition_catalogue", "show_acquisition_catalogue")}


@pytest.fixture
def stubs(local_data, monkeypatch):
    """Every section entry point replaced by a recorder: which sections ran, nothing computed."""
    import importlib
    import src.config

    monkeypatch.setattr(src.config, "DATA_DIR", local_data)
    monkeypatch.setattr("src.components.resource_panel.available_memory_bytes", lambda: None)
    recorders = {}
    for section, (module, name) in SECTIONS.items():
        recorders[section] = Mock(name=name)
        monkeypatch.setattr(importlib.import_module(module), name, recorders[section])
    return recorders


def _temporal(stubs, section=None) -> AppTest:
    app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=60).run()
    for recorder in stubs.values():   # Only what the temporal page runs is recorded.
        recorder.reset_mock()
    if section is not None:
        app.session_state["section"] = LABELS[section]
    return app.switch_page(TEMPORAL).run()


@pytest.mark.parametrize("section", list(SECTIONS))
def test_only_the_selected_section_runs(stubs, section):
    app = _temporal(stubs, section)
    assert not app.exception and not app.error
    assert app.title[0].value == "Analyses temporelles"
    assert [name for name, recorder in stubs.items() if recorder.called] == [section]


def test_default_section_is_the_comparison_of_two_dates(stubs):
    app = _temporal(stubs)
    assert app.session_state["section"] == LABELS["comparer"] and stubs["comparer"].called
    assert app.header[0].value == "Comparer deux dates"


def test_an_error_stays_inside_its_section(stubs):
    stubs["variation"].side_effect = RuntimeError("C:/Users/private/path secret")
    app = _temporal(stubs, "variation")
    assert not app.exception                                  # Caught by the section guard.
    assert app.title[0].value == "Analyses temporelles"       # The page and its selector remain.
    assert "Identifiant de diagnostic" in app.error[0].value and "private" not in app.error[0].value
    app.session_state["section"] = LABELS["serie"]
    app.run()
    assert not app.error and stubs["serie"].called


def test_section_and_study_zone_survive_a_page_switch(stubs):
    app = _temporal(stubs, "serie")
    zone = app.session_state["geonaf_zone"]
    app.switch_page("views/territoires.py").run()
    app.switch_page(TEMPORAL).run()
    assert app.session_state["section"] == LABELS["serie"] and stubs["serie"].call_args.args[1] == zone


def test_home_links_open_the_series_and_catalogue_sections(stubs):
    app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=60).run()
    links = {link.label: link for link in app.get("page_link")}
    # Deep links to a section: the section selector is bound to the "section" URL query parameter
    # (checked in a real browser, AppTest does not feed bound widgets from the URL).
    assert links["Série temporelle NDVI"].proto.query_string == "section=S%C3%A9rie+temporelle"
    assert links["Catalogue des acquisitions Sentinel-2"].proto.query_string == "section=Donn%C3%A9es+et+acquisitions"
