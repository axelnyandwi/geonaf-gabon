"""Real Streamlit registration and round trip; scientific services use fixtures."""

from pathlib import Path

from tests.test_temporal_streamlit import configured_app
from tests.test_local_preview import local_data


def test_automatic_registration_and_home_catalogue_round_trip(local_data, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    assert (root / "pages/1_Acquisitions_Sentinel2.py").is_file()
    app, prepare, analyze = configured_app(local_data, monkeypatch)
    app.run(timeout=30)
    assert not app.exception and not app.error
    assert any(link.label == "Catalogue des acquisitions Sentinel-2"
               for link in app.get("page_link"))
    original = app.session_state["result"]
    app.switch_page("pages/1_Acquisitions_Sentinel2.py").run(timeout=30)
    assert not app.exception and not app.error
    assert app.title[0].value == "Catalogue des acquisitions Sentinel-2"
    assert app.button(key="catalogue_search")
    assert any(link.label == "Accueil" for link in app.get("page_link"))
    assert not app.metric  # No scientific page executed inside the catalogue.
    app.switch_page("app.py").run(timeout=30)
    assert not app.exception and not app.error and len(app.metric) == 4
    assert app.session_state["result"] is original
    assert prepare.call_count == analyze.call_count == 1
