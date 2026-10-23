"""« Territoires et frontières » page with AppTest; referentials from a fake transport, offline."""

from pathlib import Path
import socket
from unittest.mock import Mock

import geopandas as gpd
import pytest
from shapely.geometry import box
from streamlit.testing.v1 import AppTest

from src.components.territory_explorer import KINDS
from tests.test_geoboundaries import happy_path, run
from tests.test_territory_catalog import load_both

APP = str(Path(__file__).resolve().parents[1] / "app.py")


def open_page():
    """Start from the home script (as users do) then switch: page links need the app's page registry."""
    app = AppTest.from_file(APP)
    app.run(timeout=60)
    return app.switch_page("pages/4_Territoires.py").run(timeout=60)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("Appel réseau interdit dans ces tests.")
    monkeypatch.setattr(socket, "create_connection", refuse)


@pytest.fixture
def page(tmp_path, monkeypatch):
    import src.config
    import src.services.territory_catalog as catalog

    (tmp_path / "aoi").mkdir()
    gpd.GeoDataFrame(geometry=[box(9.40, 0.40, 9.50, 0.50)], crs=4326).to_file(tmp_path / "aoi" / "akanda.geojson")
    monkeypatch.setattr(src.config, "DATA_DIR", tmp_path)
    # Explicit loads are served by the fake transport (gbOpen-like units) instead of the network.
    loader = Mock(side_effect=lambda data_dir, release, level, **kw: run(data_dir, happy_path(), release=release,
                                                                        level=level))
    monkeypatch.setattr(catalog, "load_referential", loader)
    return tmp_path, loader


def texts(app):
    return [e.value for kind in (app.info, app.warning, app.caption, app.error, app.markdown) for e in kind]


def test_historical_zone_is_shown_as_acquisition_zone(page):
    data_dir, loader = page
    app = open_page()
    assert not app.exception and not app.error and app.title[0].value == "Territoires et frontières"
    assert any("ni une commune" in t for t in texts(app))
    # OSM volume is not estimated on this page: unknown, never assumed small.
    assert any("verdict : **inconnu**" in t and "Volume OSM inconnu" in t for t in texts(app))
    assert "Zone d'acquisition GEONAF" in [str(v) for v in app.table[0].value["Valeur"]]
    assert loader.call_count == 0
    assert all(str(data_dir) not in t for t in texts(app))


def test_referential_is_loaded_only_on_click(page):
    data_dir, loader = page
    app = open_page()
    app.radio(key="territory_kind").set_value(KINDS[1]).run(timeout=60)
    app.radio(key="territory_release").set_value("gbHumanitarian").run(timeout=60)
    app.selectbox(key="territory_level").set_value("ADM1").run(timeout=60)
    assert loader.call_count == 0 and any("Référentiel non chargé" in t for t in texts(app))
    app.selectbox(key="territory_level").set_value("ADM2").run(timeout=60)
    app.radio(key="territory_release").set_value("gbOpen").run(timeout=60)
    app.button(key="load_gbOpen_ADM2").click().run(timeout=60)
    assert loader.call_count == 1 and not app.exception
    assert any("non officielles et non vérifiées" in t for t in texts(app))
    values = [str(v) for v in app.table[0].value["Valeur"]]
    assert "CC-BY-3.0" in values and "2023-12-12" in values and any("geoBoundaries" in v for v in values)


def test_large_territory_is_refused_before_any_analysis(page):
    data_dir, _ = page
    run(data_dir, happy_path())
    app = open_page()
    app.radio(key="territory_kind").set_value(KINDS[1]).run(timeout=60)
    units = app.selectbox(key="territory_unit_gbOpen_ADM2")
    komo = next(i for i, o in enumerate(units.options) if "Komo-Mondah" in o)
    units.select_index(komo).run(timeout=60)
    assert any("bloquée avant tout calcul" in e.value for e in app.error)


def test_comparison_shows_both_sources_and_unmatched_units(page):
    data_dir, loader = page
    load_both(data_dir)
    app = open_page()
    assert not app.exception
    labels = [m.label for m in app.metric]
    assert labels == ["gbOpen (km²)", "gbHumanitarian (km²)", "Partie commune (km²)", "Différence symétrique (km²)",
                      "Recouvrement (%)"]
    assert app.selectbox(key="compare_pair").value is not None
    assert any("jamais un changement temporel réel" in w.value for w in app.warning)
    markdown = " ".join(m.value for m in app.markdown)
    assert "Ogooué-Ivindo" in markdown and "Komo-Océan" in markdown
    assert loader.call_count == 0
