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
    assert sum("Non disponible" in t for t in app.markdown.values) == 7   # Every card except the study zone area.
    assert not any("—" in t for t in app.markdown.values if "font-size:1.7rem" in t)


def test_cards_show_hectares_named_denominators_nature_and_provenance(local_data, home):
    store_layers(local_data, "akanda")
    app, _ = home(local_data)
    assert not app.exception and not app.error
    texts = _texts(app)
    captions = " ".join(c.value for c in app.caption)
    assert "ha" in captions and any(t.startswith("soit ") and " ha" in t for t in texts)
    assert "% de la zone d'étude (eaux comprises)" in captions
    assert "% de la surface terrestre (hors eaux permanentes)" in captions
    markdown = " ".join(app.markdown.values)
    for badge in ("Mesure géométrique", "Observation d'une base cartographique", "Estimation"):
        assert f"badge[{badge}]" in markdown
    assert "Prédiction" not in markdown.replace("« prédiction »", "")
    assert len([e for e in app.expander if e.label == "Provenance et fiabilité"]) == 8
    assert "Complétude OpenStreetMap non évaluée" in markdown            # Reliability reasons are shown.
    assert "© OpenStreetMap contributors" in captions and "ODbL 1.0" in captions
    assert "© ESA WorldCover project 2021 / Contains modified Copernicus Sentinel data (2021)" in captions
    roads = next(e for e in app.expander if e.label == "Voies cartographiées par catégorie (OSM)")
    assert "Rues de desserte locale" in roads.dataframe[0].value["Catégorie"].tolist()
    assert any("rue résidentielle (residential)" in v for v in roads.dataframe[0].value["Valeurs OSM « highway » (km)"])
    assert any(c.value.startswith("**Rues de desserte locale**") for c in roads.caption)


def test_unreadable_layer_degrades_to_a_warning(local_data, home):
    store_layers(local_data, "akanda")
    target = next((local_data / "external" / "worldcover").glob("*.tif"))
    target.write_bytes(target.read_bytes() + b"x")   # Digest mismatch with the catalogue record.
    app, _ = home(local_data)
    assert not app.exception and not app.error and len(app.metric) == 4
    assert any("Chiffres clés indisponibles" in w.value for w in app.warning)


@pytest.mark.parametrize("snapshot, shown", [("2026-10-22T16:57:05Z", "22/10/2026 16:57"),
                                             ("2025-03-04T08:05:00Z", "04/03/2025 08:05")])
def test_land_density_names_both_sources_with_their_own_dates(local_data, home, monkeypatch, snapshot, shown):
    import tests.test_territorial_indicators as fixtures

    monkeypatch.setattr(fixtures, "SNAPSHOT", snapshot)                 # Date read from the metadata.
    store_layers(local_data, "akanda")
    app, _ = home(local_data)
    assert not app.exception and not app.error
    captions = [c.value for c in app.caption]
    ratio = next(c for c in captions if c.startswith("Numérateur"))
    assert f"Numérateur : bâtiments OpenStreetMap — Base OpenStreetMap au {shown} UTC" in ratio
    assert "Dénominateur : surface terrestre ESA WorldCover 10 m 2021 (v200) — carte 2021" in ratio
    assert "ce rapport ne décrit pas une même année d'observation" in ratio
    assert sum(c.startswith("Numérateur") for c in captions) == 1          # Only the land density card.
    details = " ".join(app.markdown.values)
    assert "**Dénominateur** : surface terrestre, ESA WorldCover 10 m 2021 (v200)" in details
    assert "**Dates du dénominateur** : Observation : 2021" in details
    assert "**Attribution du dénominateur** : © ESA WorldCover project 2021" in details
