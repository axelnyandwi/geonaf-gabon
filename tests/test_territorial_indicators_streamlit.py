"""« Chiffres clés du territoire » on the home page; offline, synthetic stored layers."""

from pathlib import Path
from unittest.mock import Mock

import pytest
from streamlit.testing.v1 import AppTest

from tests.test_local_preview import local_data  # noqa: F401  (fixture)
from tests.test_territorial_indicators import store_layers

APP = str(Path(__file__).resolve().parents[1] / "app.py")


@pytest.fixture
def home(monkeypatch):
    import src.config
    import src.components.territorial_indicators as ui
    import src.services.territorial_indicators as service

    def run(data_dir, **state):
        monkeypatch.setattr(src.config, "DATA_DIR", data_dir)
        ui.cached_indicators.clear()
        compute = Mock(wraps=service.compute_territorial_indicators)
        monkeypatch.setattr(ui, "compute_territorial_indicators", compute)
        app = AppTest.from_file(APP)
        for key, value in state.items():
            app.session_state[key] = value
        return app.run(timeout=60), compute
    return run


def _texts(app):
    return [e.value for kind in (app.markdown, app.caption, app.info, app.warning) for e in kind]


def test_key_figures_come_first_and_keep_the_scientific_analyses(local_data, home):
    store_layers(local_data, "akanda")
    app, compute = home(local_data)
    assert not app.exception and not app.error
    assert [h.value for h in app.header] == ["Chiffres clés du territoire", "Analyses scientifiques"]
    assert app.info[0].value.startswith("Zone d'étude Akanda") and "limite administrative" in app.info[0].value
    texts = _texts(app)
    assert any("Bâtiments cartographiés (OSM)" == t for t in texts)
    assert any("Fiabilité : non évaluée" in t for t in texts) and any("Fiabilité : moyenne" in t for t in texts)
    assert any("Référentiel : Base OpenStreetMap au 22/10/2026 16:57 UTC" in t for t in texts)
    assert any("pas le total réel" in t for t in texts)
    assert len(app.metric) == 4 and compute.call_count == 1   # Pair analysis unchanged below.
    assert all(str(local_data) not in t for t in texts)


def test_analysis_mode_does_not_change_or_recompute_key_figures(local_data, home):
    store_layers(local_data, "akanda")
    app, compute = home(local_data)
    before = [e.value for e in app.markdown]
    app.radio(key="analysis_mode").set_value("Analyse avancée").run(timeout=60)
    assert not app.exception
    assert [e.value for e in app.markdown][:len(before) // 2] == before[:len(before) // 2]
    assert compute.call_count == 1   # Cached on layer fingerprints, independent of the SCL profile.


def test_missing_sources_are_stated_without_values(local_data, home):
    app, compute = home(local_data)
    assert not app.exception and not app.error and len(app.metric) == 4
    texts = _texts(app)
    assert any("Sources absentes localement : ESA WorldCover, OpenStreetMap" in t for t in texts)
    assert sum("—" in t for t in app.markdown.values) == 7   # Every card except the study zone area.


def test_unreadable_layer_degrades_to_a_warning(local_data, home):
    store_layers(local_data, "akanda")
    target = next((local_data / "external" / "worldcover").glob("*.tif"))
    target.write_bytes(target.read_bytes() + b"x")   # Digest mismatch with the catalogue record.
    app, _ = home(local_data)
    assert not app.exception and not app.error and len(app.metric) == 4
    assert any("Chiffres clés indisponibles" in w.value for w in app.warning)
