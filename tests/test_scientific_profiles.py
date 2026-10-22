"""Scientific interpretation contracts; calendars below are synthetic."""

from dataclasses import replace
from datetime import datetime, timezone

import pytest

from src.geo.scientific_profile import (
    GABON_EQUATORIAL_EXPERIMENTAL, ScientificProfile, SeasonalRules,
)


def test_profile_is_immutable_and_all_settings_affect_identity():
    profile = GABON_EQUATORIAL_EXPERIMENTAL
    assert profile.experimental
    assert profile.seasonal_rules.month_seasons == ()
    for field, value in (("version", "2"), ("aoi_id", "owendo"),
                         ("region", "other"), ("climate", "other"),
                         ("ecosystem", "other"), ("usage", "other"),
                         ("change_threshold", 0.2), ("min_comparable_coverage_percent", 60),
                         ("scl_policy", "vegetation"),
                         ("seasonal_rules", SeasonalRules(require_same_season=False))):
        assert replace(profile, **{field: value}).fingerprint != profile.fingerprint


@pytest.mark.parametrize("field,value", [
    ("change_threshold", 0), ("change_threshold", float("nan")),
    ("change_threshold", True), ("min_comparable_coverage_percent", 101),
    ("min_comparable_coverage_percent", -1), ("scl_policy", "unknown"),
    ("version", ""), ("aoi_id", "../outside"),
])
def test_profile_rejects_invalid_configuration(field, value):
    with pytest.raises(ValueError):
        replace(GABON_EQUATORIAL_EXPERIMENTAL, **{field: value})


def test_calendar_is_explicit_sourced_and_uses_utc_month():
    with pytest.raises(ValueError):
        SeasonalRules(month_seasons=("A",) * 12)
    profile = replace(GABON_EQUATORIAL_EXPERIMENTAL, seasonal_rules=SeasonalRules(
        month_seasons=("A",) * 6 + ("B",) * 6, source="synthetic-calendar", version="1"))
    context = profile.seasonal_context(datetime(2025, 7, 1, tzinfo=timezone.utc))
    assert context.season_id == "B"
    assert context.source == "synthetic-calendar"
    assert context.region == profile.region
    assert context.vegetation_type == profile.ecosystem
