"""Profile propagation, conservative interpretation and cache invalidation."""

from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path

import geopandas as gpd
import numpy as np
import pytest
from shapely.geometry import box

from src.geo.scientific_profile import (
    GABON_EQUATORIAL_EXPERIMENTAL as BASE, GABON_SENSITIVITY_EXPERIMENTAL,
    SeasonalRules, ScientificStatus,
)
from src.services.temporal_analysis import analyze_temporal_vegetation_change
from src.components.preview_cache import peek_pair_analysis
from src.services.local_preview import analyze_local_akanda, list_local_observations, local_preview_key
from src.data.acquisition import SeasonalContext
from tests.test_local_preview import local_data
from tests.test_temporal_acquisition_integration import _make_acquisition, PRODUCT_A, PRODUCT_B


@pytest.fixture
def profile_pair(tmp_path):
    acquisitions = tuple(
        _make_acquisition(tmp_path, product, datetime(2025, 1, day, tzinfo=timezone.utc),
                          red_dn=2000, nir_dn=nir, red_offset=0, nir_offset=0,
                          scl_values=np.full((4, 4), 4, dtype=np.uint8))
        for product, day, nir in ((PRODUCT_A, 1, 2000), (PRODUCT_B, 11, 2700))
    )
    aoi = tmp_path / "aoi.gpkg"
    gpd.GeoDataFrame(geometry=[box(0, 0, 4, 4)], crs="EPSG:6933").to_file(aoi)
    return acquisitions, aoi


def _calendar_profile(**overrides):
    return replace(BASE, seasonal_rules=SeasonalRules(
        month_seasons=("A",) * 6 + ("B",) * 6, source="synthetic-calendar", version="1"),
        **overrides)


def test_two_profiles_change_classes_without_changing_ndvi_and_preserve_provenance(profile_pair):
    pair, aoi = profile_pair
    first_profile = _calendar_profile()
    second_profile = replace(first_profile, profile_id="synthetic-second", change_threshold=0.2)
    first, second = [analyze_temporal_vegetation_change(*pair, aoi_path=aoi, scientific_profile=p)
                     for p in (first_profile, second_profile)]
    np.testing.assert_array_equal(first.comparison.delta_ndvi, second.comparison.delta_ndvi)
    assert first.analysis.class_pixel_counts["increase"] == 16
    assert second.analysis.class_pixel_counts["stable"] == 16
    assert first.analysis.comparable_area_m2 == pytest.approx(16)
    assert first.scientific_profile == first_profile
    assert second.scientific_profile.fingerprint != first.scientific_profile.fingerprint
    assert first.aoi_sha256 and first.aoi_sha256 == second.aoi_sha256
    assert first.earlier.seasonal_context.source == "synthetic-calendar"
    assert first.interpretation.status == ScientificStatus.EXPLORATION


@pytest.mark.parametrize("scenario,expected", [
    ("missing", "not_assessed"), ("different", "not_comparable"),
    ("same", "exploration"), ("long_interval", "not_comparable"),
    ("no_experimental_flag", "exploration"), ("interseason_allowed", "exploration"),
])
def test_seasonal_status_never_validates_durable_change(profile_pair, scenario, expected):
    pair, aoi = profile_pair
    profile = BASE if scenario == "missing" else _calendar_profile()
    if scenario in {"different", "interseason_allowed"}:
        pair = (pair[0], replace(pair[1], acquired_at=datetime(2025, 7, 11, tzinfo=timezone.utc)))
    if scenario == "long_interval":
        profile = replace(profile, seasonal_rules=replace(profile.seasonal_rules, max_interval_days=5))
    if scenario == "no_experimental_flag":
        profile = replace(profile, experimental=False)
    if scenario == "interseason_allowed":
        profile = replace(profile, seasonal_rules=replace(profile.seasonal_rules, require_same_season=False))
    result = analyze_temporal_vegetation_change(*pair, aoi_path=aoi, scientific_profile=profile)
    assert result.interpretation.status.value == expected
    assert result.interpretation.status != ScientificStatus.VALIDATED
    assert result.analysis.statistics["count"] == 16  # Restrictions never suppress exploration.
    assert any("aucun changement durable validé" in message for message in result.interpretation.restrictions)
    if scenario in {"different", "interseason_allowed"}:
        assert result.seasonal_assessment.season_relation == "different"
    if scenario == "same":
        assert result.seasonal_assessment.season_relation == "same"


@pytest.mark.parametrize("policy", ["observation", "land_surface", "vegetation", "bare_soil"])
def test_unsafe_policies_cannot_qualify_land_change(profile_pair, policy):
    pair, aoi = profile_pair
    result = analyze_temporal_vegetation_change(
        *pair, aoi_path=aoi, scientific_profile=_calendar_profile(scl_policy=policy))
    assert result.interpretation.status == ScientificStatus.NOT_COMPARABLE
    assert result.interpretation.restrictions


def test_coverage_gate_and_empty_comparison_keep_exploratory_arrays(profile_pair):
    pair, aoi = profile_pair
    profile = _calendar_profile(min_comparable_coverage_percent=100)
    larger_aoi = aoi.with_name("larger.gpkg")
    gpd.GeoDataFrame(geometry=[box(0, 0, 8, 4)], crs="EPSG:6933").to_file(larger_aoi)
    result = analyze_temporal_vegetation_change(*pair, aoi_path=larger_aoi, scientific_profile=profile)
    assert result.analysis.comparable_coverage_percent == pytest.approx(50)
    assert result.interpretation.status == ScientificStatus.NOT_COMPARABLE
    assert result.analysis.statistics["count"] == 16


@pytest.mark.parametrize("parameters", [
    {"threshold": 0.2}, {"scl_policy": "vegetation"}, {"seasonal_evaluator": object()},
])
def test_conflicting_overrides_fail_before_processing(profile_pair, parameters):
    pair, aoi = profile_pair
    with pytest.raises(ValueError):
        analyze_temporal_vegetation_change(*pair, aoi_path=aoi, scientific_profile=BASE, **parameters)


def test_profile_requires_aoi_scoped_coverage(profile_pair):
    pair, _ = profile_pair
    with pytest.raises(ValueError, match="AOI"):
        analyze_temporal_vegetation_change(*pair, scientific_profile=BASE)


@pytest.mark.parametrize("field,value", [("region", "other"), ("vegetation_type", "other"),
                                        ("source", "other"), ("version", "2")])
def test_external_contexts_must_match_profile_and_source(profile_pair, field, value):
    pair, aoi = profile_pair
    context = SeasonalContext(BASE.region, BASE.ecosystem, "A", "synthetic-context", "1")
    pair = (replace(pair[0], seasonal_context=context),
            replace(pair[1], seasonal_context=replace(context, **{field: value})))
    result = analyze_temporal_vegetation_change(*pair, aoi_path=aoi, scientific_profile=BASE)
    assert result.interpretation.status == ScientificStatus.NOT_ASSESSED
    assert result.later.seasonal_context == pair[1].seasonal_context


def test_calendar_cannot_silently_override_supplied_context(profile_pair):
    pair, aoi = profile_pair
    context = SeasonalContext(BASE.region, BASE.ecosystem, "B", "other-calendar", "1")
    pair = (replace(pair[0], seasonal_context=context), pair[1])
    with pytest.raises(ValueError, match="contredit"):
        analyze_temporal_vegetation_change(*pair, aoi_path=aoi, scientific_profile=_calendar_profile())


@pytest.mark.parametrize("field,value", [
    ("version", "2"), ("climate", "other"), ("region", "other"),
    ("ecosystem", "other"), ("usage", "other"), ("change_threshold", 0.2),
    ("min_comparable_coverage_percent", 75),
    ("seasonal_rules", SeasonalRules(require_same_season=False)),
])
def test_cache_invalidates_all_profile_changes(local_data, field, value):
    pair = list_local_observations(local_data)
    key = local_preview_key(*pair, local_data, scientific_profile=BASE)
    assert key != local_preview_key(*pair, local_data, scientific_profile=replace(BASE, **{field: value}))


def test_profile_aoi_controls_processing_and_cache(local_data):
    pair = list_local_observations(local_data)
    path = local_data / "aoi" / "owendo.geojson"
    path.write_bytes((local_data / "aoi" / "akanda.geojson").read_bytes())
    profile = replace(BASE, aoi_id="owendo")
    result = analyze_local_akanda(*pair, local_data, scientific_profile=profile)
    assert result.aoi_path == str(path.resolve())
    key = local_preview_key(*pair, local_data, scientific_profile=profile)
    path.write_bytes(path.read_bytes() + b"\n")
    assert key != local_preview_key(*pair, local_data, scientific_profile=profile)


def test_akanda_profile_preserves_existing_science(local_data):
    pair = list_local_observations(local_data)
    original = analyze_local_akanda(*pair, local_data)
    profiled = analyze_local_akanda(*pair, local_data, scientific_profile=BASE)
    for name in ("ndvi_date_1", "ndvi_date_2", "comparison_mask", "delta_ndvi"):
        np.testing.assert_array_equal(getattr(original.comparison, name), getattr(profiled.comparison, name))
    np.testing.assert_array_equal(original.analysis.change_class_map, profiled.analysis.change_class_map)
    assert original.analysis.statistics == profiled.analysis.statistics
    assert original.analysis.areas_m2 == profiled.analysis.areas_m2
    assert original.earlier == profiled.earlier
    assert profiled.interpretation.status == ScientificStatus.NOT_ASSESSED


def test_public_and_exported_provenance_and_map_cache_follow_profile(local_data, tmp_path):
    from src.services.temporal_map import TemporalMapCache
    from src.services.geotiff_export import export_temporal_change_geotiff

    pair = list_local_observations(local_data)
    first, second = [analyze_local_akanda(*pair, local_data, scientific_profile=p)
                     for p in (BASE, replace(BASE, version="2"))]
    cache = TemporalMapCache()
    # Even an accidentally reused caller key must not reuse another profile's metadata.
    a = cache.prepare(first, scientific_key="same", max_size=2)
    b = cache.prepare(second, scientific_key="same", max_size=2)
    assert a["result_id"] != b["result_id"]
    assert b["provenance"]["scientific"]["profile"]["fingerprint"] == second.scientific_profile.fingerprint
    assert b["provenance"]["scientific"]["interpretation_status"] == "not_assessed"
    assert str(local_data) not in json.dumps(b)
    # The existing export writes full traceability; it must include these restrictions.
    export_temporal_change_geotiff(second, tmp_path / "profiled.tif")
    manifest = json.loads((tmp_path / "profiled_manifest.json").read_text())
    assert manifest["scientific_profile"]["version"] == "2"
    assert manifest["interpretation"]["status"] == "not_assessed"


def test_streamlit_profile_selection_invalidates_science_and_displays_restrictions(local_data, monkeypatch):
    from streamlit.testing.v1 import AppTest
    import src.config

    monkeypatch.setattr(src.config, "DATA_DIR", local_data)
    app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py")).run(timeout=30)
    assert not app.exception and not app.error
    first_key = app.session_state["pair"]
    assert peek_pair_analysis(app.session_state["pair"]).scientific_profile == BASE
    assert any("Comparabilité non évaluée" in message.value for message in app.warning)
    # Advanced mode with the sensitivity preset derives a new versioned profile.
    app.radio(key="analysis_mode").set_value("Analyse avancée").run(timeout=30)
    assert peek_pair_analysis(app.session_state["pair"]).scientific_profile == BASE  # Unchanged values: no derived profile.
    app.button(key="analysis_preset").click().run(timeout=30)
    assert not app.exception and not app.error
    assert app.session_state["pair"] != first_key
    derived = peek_pair_analysis(app.session_state["pair"]).scientific_profile
    assert peek_pair_analysis(app.session_state["pair"]).threshold == 0.15 and derived.min_comparable_coverage_percent == 50.0
    assert derived.fingerprint != BASE.fingerprint and derived.experimental
    assert derived.version.endswith(".advanced-1") and BASE.fingerprint[:16] in derived.rationale
    options = app.selectbox(key="analysis_scl").options  # Formatted labels; cloudy policies never offered.
    assert len(options) == 3 and not any("nuage" in option.lower() or "observation" in option for option in options)
    assert any("non validée" in message.value for message in app.warning)
    assert len(app.metric) == 4
    app.radio(key="analysis_mode").set_value("Analyse standard").run(timeout=30)
    assert peek_pair_analysis(app.session_state["pair"]).scientific_profile == BASE and app.session_state["pair"] == first_key
