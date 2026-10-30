"""« Végétation observée par Sentinel-2 » on the home page with AppTest; offline."""

from dataclasses import replace
from unittest.mock import Mock

import pytest
from streamlit.testing.v1 import AppTest

from src.components.temporal_guide import VARIATION_TITLE
from src.indicators.profiles import S2_NDVI_VEGETATION_V0
from tests.test_local_preview import local_data  # noqa: F401  (fixture)
from tests.test_s2_vegetation_inventory import PACK
from tests.test_territorial_indicators import store_layers
from tests.test_territorial_indicators_streamlit import APP

START, DATE = "s2_vegetation_start_akanda", "s2_vegetation_date_akanda"


@pytest.fixture
def home(monkeypatch):
    import src.config
    import src.components.s2_vegetation as section
    import src.components.territorial_indicators as figures

    def run(data_dir, profile=S2_NDVI_VEGETATION_V0):
        monkeypatch.setattr(src.config, "DATA_DIR", data_dir)
        monkeypatch.setattr("src.components.resource_panel.available_memory_bytes", lambda: None)
        monkeypatch.setattr(section, "VEGETATION_PROFILE", profile)
        figures.cached_indicators.clear()
        section.cached_inventory.clear()
        compute = Mock(wraps=section.s2_vegetation_inventory)
        monkeypatch.setattr(section, "s2_vegetation_inventory", compute)
        return AppTest.from_file(APP).run(timeout=120), compute
    return run


def texts(app):
    return [e.value for kind in (app.markdown, app.caption, app.info, app.warning) for e in kind]


def section_texts(app):
    """Texts between the section title and the next header, in document order."""
    collected, inside = [], False
    for node in app.main:
        try:
            value = node.value
        except Exception:
            continue
        if not isinstance(value, str):
            continue
        if value == "Végétation observée par Sentinel-2":
            inside = True
        elif value == "Comparer deux dates":
            inside = False
        elif inside:
            collected.append(value)
    return " ".join(collected)


def test_nothing_is_computed_at_the_initial_load(local_data, home):
    store_layers(local_data, "akanda")
    app, compute = home(local_data)
    assert not app.exception and not app.error
    assert "Végétation observée par Sentinel-2" in [s.value for s in app.subheader]
    assert any("Estimation expérimentale" in t for t in app.markdown.values)
    assert any("ni entre acquisitions dont les masques d'observation diffèrent" in w.value for w in app.warning)
    assert compute.call_count == 0 and app.button(key=START).label == "Calculer l'estimation Sentinel-2"
    assert [h.value for h in app.header] == ["Chiffres clés du territoire", "Comparer deux dates", VARIATION_TITLE]


def test_explicit_start_computes_once_and_date_changes_reuse_the_cache(local_data, home):
    app, compute = home(local_data)
    app.button(key=START).click().run(timeout=120)
    assert not app.exception and compute.call_count == 1
    assert START not in [b.key for b in app.button]                                   # Gone once clicked.
    selector = app.selectbox(key=DATE)
    assert selector.value == 0 and "25/01/2025" in selector.format_func(0)            # Most recent publishable.
    captions = " ".join(c.value for c in app.caption)
    assert "soit " in captions and " ha" in captions and "% de la surface observée exploitable" in captions
    assert "Surface observée exploitable" in captions and "% de la zone d'étude" in captions
    assert "Observée mais exclue" in captions and "non observée (nuages, ombres)" in captions
    assert "seuil NDVI ≥ 0,4" in captions and "Copernicus Sentinel-2 L2A" in captions
    markdown = " ".join(app.markdown.values)
    assert "badge[Expérimental]" in markdown and "badge[Fiabilité : faible]" in markdown
    assert {e.label for e in app.expander} >= {"Ventilation de la zone d'étude et définitions",
                                               "Sensibilité au seuil NDVI (0,3 · 0,4 · 0,5)",
                                               "Provenance, profils, méthode et limites"}
    app.selectbox(key=DATE).set_value(1).run(timeout=120)                             # 15/01: a rerun.
    assert not app.exception and compute.call_count == 1                               # Cached aggregate.
    assert "15/01/2025" in app.selectbox(key=DATE).format_func(1)


def test_unpublishable_estimate_is_not_presented_as_usable(local_data, home):
    strict = replace(S2_NDVI_VEGETATION_V0, min_observed_coverage_percent=60.0)
    app, _ = home(local_data, profile=strict)
    app.button(key=START).click().run(timeout=120)
    assert not app.exception
    part = section_texts(app)
    assert "Non disponible" in part and "badge[Non publiable]" in part and "60 %" in part
    assert "soit " not in part and "% de la surface observée exploitable" not in part   # No published figures.
    assert "non publiable" in app.selectbox(key=DATE).format_func(0)
    labels = [e.label for e in app.expander]
    assert "Données de diagnostic (non publiables)" in labels
    assert not any(label.startswith("Sensibilité") for label in labels)            # Only inside the diagnosis.
    diagnosis = next(e for e in app.expander if e.label == "Données de diagnostic (non publiables)")
    assert diagnosis.dataframe[0].value["Seuil"].tolist() == ["NDVI ≥ 0,3", "NDVI ≥ 0,4", "NDVI ≥ 0,5"]
    assert any("Aucune acquisition n'atteint la couverture minimale" in c.value for c in app.caption)


def test_categories_name_their_cause_without_geography_and_attribution_is_flagged(local_data, home):
    app, _ = home(local_data)
    app.button(key=START).click().run(timeout=120)
    breakdown = next(e for e in app.expander if e.label == "Ventilation de la zone d'étude et définitions")
    frame = breakdown.dataframe[0].value
    labels = frame["Catégorie"].tolist()
    assert "Sans donnée (SCL absent ou hors de la grille analysée)" in labels
    assert not any("emprise" in label.lower() for label in labels)
    definitions = {c.value.split("** — ")[0].strip("*"): c.value.split("** — ")[1]
                   for c in breakdown.caption if "** — " in c.value}
    assert set(definitions) == set(labels)                                            # One definition each.
    assert "n'est pas déterminée" in definitions["Sans donnée (SCL absent ou hors de la grille analysée)"]
    assert any("selon le SCL" in label for label in labels)
    provenance = next(e for e in app.expander if e.label == "Provenance, profils, méthode et limites")
    assert any(c.value.startswith("Attribution non revérifiée") for c in provenance.caption)
    sensitivity = next(e for e in app.expander if e.label.startswith("Sensibilité"))
    assert sensitivity.dataframe[0].value["Seuil"].tolist() == ["NDVI ≥ 0,3", "NDVI ≥ 0,4", "NDVI ≥ 0,5"]


def test_pack_v3_defaults_to_25_january_and_shows_20_january_as_unavailable(home):
    if not (PACK / "PACK_MANIFEST.json").is_file():
        pytest.skip("Paquet de démonstration absent.")
    app, compute = home(PACK)
    app.button(key=START).click().run(timeout=120)
    assert not app.exception and compute.call_count == 1
    selector = app.selectbox(key=DATE)
    options = [selector.format_func(i) for i in range(3)]                      # Most recent first.
    assert selector.value == 0 and options[0].startswith("25/01/2025")
    assert options[1].startswith("20/01/2025") and options[1].endswith("non publiable")
    assert "61,61" in section_texts(app) and "66,28 % de la surface observée exploitable" in section_texts(app)
    app.selectbox(key=DATE).set_value(1).run(timeout=120)
    part = section_texts(app)
    assert compute.call_count == 1 and "Non disponible" in part and "2,7 % < 30 %" in part
    assert "61,61" not in part.split("Données de diagnostic")[0]


def test_sentinel2_tables_are_numeric_with_full_precision(local_data, home):
    app, _ = home(local_data)
    app.button(key=START).click().run(timeout=120)
    breakdown = next(e for e in app.expander if e.label == "Ventilation de la zone d'étude et définitions")
    frame = breakdown.dataframe[0].value
    assert all(str(frame[c].dtype) == "float64" for c in ("Surface (km²)", "Surface (ha)",
                                                           "Part de la zone d'étude (%)"))
    assert frame["Surface (ha)"].tolist() == [v * 100.0 for v in frame["Surface (km²)"]]
    sensitivity = next(e for e in app.expander if e.label.startswith("Sensibilité"))
    assert str(sensitivity.dataframe[0].value["Part de la surface observée (%)"].dtype) == "float64"
