"""Real Streamlit registration and round trip; scientific services use fixtures."""

from pathlib import Path

from src.components.preview_cache import peek_pair_analysis

from tests.streamlit_pages import TEMPORAL, open_section
from tests.test_temporal_streamlit import configured_app
from tests.test_local_preview import local_data


def test_router_registration_and_variation_catalogue_round_trip(local_data, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    assert (root / TEMPORAL).is_file() and not (root / "pages").exists()   # st.navigation only, no pages/.
    app, prepare, analyze = configured_app(local_data, monkeypatch)
    app.run(timeout=30)
    assert not app.exception and not app.error
    assert app.title[0].value == "Tableau de bord"
    assert any(link.label == "Catalogue des acquisitions Sentinel-2" for link in app.get("page_link"))
    assert prepare.call_count == analyze.call_count == 0          # No pair analysis on the dashboard.
    open_section(app, "variation", timeout=30)
    assert not app.exception and not app.error and len(app.metric) == 4
    original = peek_pair_analysis(app.session_state["pair"])
    open_section(app, "donnees", timeout=30)
    assert not app.exception and not app.error
    assert app.title[0].value == "Analyses temporelles"
    assert app.header[0].value == "Catalogue des acquisitions Sentinel-2"
    assert app.button(key="catalogue_search")
    assert not app.get("page_link")   # Back to other pages through the sidebar navigation (st.navigation).
    assert not app.metric             # No scientific section executed inside the catalogue.
    open_section(app, "variation", timeout=30)
    assert not app.exception and not app.error and len(app.metric) == 4
    assert peek_pair_analysis(app.session_state["pair"]) is original
    assert prepare.call_count == analyze.call_count == 1
