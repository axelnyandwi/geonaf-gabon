"""Display vocabulary, hectares, result natures and verified attributions; offline."""

import re

import pytest

from src.components.formatting import format_hectares_from_km2, format_percent
from src.data.openstreetmap import LICENSE as OSM_LICENSE
from src.data.worldcover import LICENSE as WORLDCOVER_LICENSE
from src.indicators.model import (
    ESTIMATION, GEOMETRIC_MEASURE, MAPPED_OBSERVATION, PREDICTION, RESULT_KINDS, IndicatorSource,
)
from src.indicators.presentation import (
    RESULT_KIND_TEXT, ROAD_GLOSSARY, attribution, highway_label, road_category_label, share_text,
)
from src.indicators.profiles import AFRICA_DEFAULT_V1, OTHER_ROADS
from src.services.territorial_indicators import build_indicators
from tests.test_indicators_modules import AFRICA_DEFAULT_V1_FINGERPRINT
from tests.test_territorial_indicators import (  # noqa: F401  (fixture)
    aoi, aoi_bounds, landcover_codes, osm_descriptor, osm_frame, worldcover_layer, write_landcover,
)

EXPECTED_KINDS = {
    "aoi.area_km2": GEOMETRIC_MEASURE,
    "landcover.land_km2": ESTIMATION, "landcover.vegetation_km2": ESTIMATION, "landcover.builtup_km2": ESTIMATION,
    "osm.roads_km": MAPPED_OBSERVATION, "osm.buildings_count": MAPPED_OBSERVATION,
    "osm.buildings_footprint_km2": MAPPED_OBSERVATION, "osm.buildings_density_aoi": MAPPED_OBSERVATION,
    "osm.buildings_density_land": ESTIMATION,
}


@pytest.mark.parametrize("km2, text", [(1.0, "100"), (82.58, "8 258"), (0.012, "1,20"), (0.0, "0,00"),
                                       (None, "—")])
def test_hectares_are_a_display_of_km2(km2, text):
    assert format_hectares_from_km2(km2) == text


def test_denominators_are_named():
    assert share_text("zone d'étude", 57.3, format_percent) == "57,30 % de la zone d'étude (eaux comprises)"
    assert share_text("surface terrestre", 76.86, format_percent).endswith("(hors eaux permanentes)")


@pytest.mark.parametrize("with_sources", [True, False], ids=["sources présentes", "sources absentes"])
def test_every_indicator_has_its_explicit_nature_and_none_is_a_prediction(tmp_path, aoi, with_sources):
    kwargs = {}
    if with_sources:
        kwargs = dict(worldcover=[worldcover_layer(write_landcover(tmp_path / "wc.tif", landcover_codes()))],
                      osm=(osm_frame([]), osm_descriptor(aoi_bounds(aoi))))
    result = build_indicators(aoi_id="test", aoi_path=aoi, **kwargs)
    assert {i.indicator_id: i.result_kind for i in result.indicators} == EXPECTED_KINDS
    assert all(i.result_kind in RESULT_KINDS and i.result_kind != PREDICTION for i in result.indicators)
    assert set(RESULT_KIND_TEXT) == set(RESULT_KINDS)


def test_glossary_covers_the_profile_without_changing_it():
    labels = [label for label, _ in AFRICA_DEFAULT_V1.road_categories] + [OTHER_ROADS]
    assert set(ROAD_GLOSSARY) == set(labels) and all(ROAD_GLOSSARY[label][1] for label in labels)
    assert road_category_label("Voies de desserte") != road_category_label("Voies de service")
    assert AFRICA_DEFAULT_V1.fingerprint == AFRICA_DEFAULT_V1_FINGERPRINT


def test_glossary_does_not_assert_access_or_surface_it_cannot_know():
    service, track = ROAD_GLOSSARY["Voies de service"][1], ROAD_GLOSSARY["Pistes"][1]
    assert not re.search(r"\bprivée?s?\b|interdite?s?|réservée?s?", service.replace("n'est pas déduit", ""))
    assert "n'est pas déduit" in service
    assert "revêtement n'est pas déduit" in track and "non revêtue" not in track


def test_osm_values_are_translated_and_unknown_values_stay_raw():
    assert highway_label("unclassified") == "voie mineure (unclassified)"
    assert highway_label("road") == "type non précisé (road)"
    assert highway_label("busway") == "busway"


def _source(source_id, version="v200", text="stored text"):
    return IndicatorSource(source_id, "product", version, "nature", text, None)


def test_worldcover_attribution_follows_the_producer_template_with_the_map_year():
    credit = attribution(_source("esa-worldcover"), 2021)
    assert credit.text == ("© ESA WorldCover project 2021 / Contains modified Copernicus Sentinel data (2021) "
                           "processed by ESA WorldCover consortium")
    assert credit.verified and credit.citation == "https://doi.org/10.5281/zenodo.7254221"
    assert attribution(_source("esa-worldcover", "v100"), 2020).citation.endswith("5571936")


def test_osm_unknown_and_zone_attributions():
    osm = attribution(_source("openstreetmap"), None)
    assert osm.text == "© OpenStreetMap contributors" and osm.licence_url == "https://www.openstreetmap.org/copyright"
    other = attribution(_source("sentinel-1", text="Contains modified Copernicus Sentinel data"), 2025)
    assert not other.verified and other.text == "Contains modified Copernicus Sentinel data"
    assert attribution(_source("geonaf-aoi"), None) is None and attribution(None, None) is None


def test_displayed_licences_match_the_ingestion_licences():
    names = {"CC-BY-4.0": "CC BY 4.0", "ODbL-1.0": "ODbL 1.0"}
    assert attribution(_source("esa-worldcover"), 2021).licence == names[WORLDCOVER_LICENSE.spdx_id]
    assert attribution(_source("openstreetmap"), None).licence == names[OSM_LICENSE.spdx_id]


# --- French decimals (L3e) ----------------------------------------------------------------------------------


def test_french_table_formats_display_only_and_keeps_numbers():
    import pandas as pd

    from src.components.formatting import french_table

    frame = pd.DataFrame({"Catégorie": ["a", "b", "c"], "Surface (km²)": [1234.5678, 0.5, None],
                          "Part (%)": [57.3, 100.0, None]})
    styler = french_table(frame, {"Surface (km²)": 2, "Part (%)": 2, "Absente": 1})
    text = styler.to_string()
    assert "1 234,57" in text and "0,50" in text and "57,30" in text and "100,00" in text
    assert text.count("—") == 2 and "." not in text.replace("Surface (km²)", "")    # No decimal point left.
    assert styler.data is frame and frame["Surface (km²)"].iloc[0] == 1234.5678       # Numbers untouched.
    assert str(frame["Surface (km²)"].dtype) == "float64"                             # Numeric sort preserved.


def test_landcover_reliability_reasons_use_french_decimals():
    from types import SimpleNamespace
    from datetime import datetime as dt

    from src.indicators.quality import landcover_reliability

    descriptor = SimpleNamespace(temporal=SimpleNamespace(start_utc=dt(2021, 1, 1)))
    level, reasons, warning = landcover_reliability(descriptor, 50.04, AFRICA_DEFAULT_V1)
    assert level == "faible" and "Couverture 50,0 % < 99 % de la zone d'étude." in reasons
    assert warning == "WorldCover ne couvre que 50,0 % de la zone d'étude : surfaces sous-estimées."


def test_change_assessment_reason_uses_french_decimals(tmp_path, aoi):
    from dataclasses import replace

    from src.indicators.quality import assess_change

    result = build_indicators(aoi_id="test", aoi_path=aoi, worldcover=[
        worldcover_layer(write_landcover(tmp_path / "wc.tif", landcover_codes()))])
    land = result.get("landcover.land_km2")
    change = assess_change(replace(land, coverage_percent=50.04), land)
    assert "Couverture insuffisante (50,0 %)." in change.reasons
