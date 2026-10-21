"""Exact UTC calendar matching; no nearest-date or automatic quality ranking."""

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from src.components.observation_picker import observation_quality_notices
from src.services.local_preview import LocalObservation, LocalPreviewError
from src.services.observation_selection import (
    acquisition_day, catalogue_bounds, observation_label, observations_on_day,
    resolve_observation, validate_chronology,
)


def observation(day, *, hour=9, product='product'):
    return LocalObservation(datetime(2025, 1, day, hour, tzinfo=timezone.utc), Path('private')/product, product)


def test_exact_dates_and_catalogue_bounds():
    first, last = observation(15, product='first'), observation(25, product='last')
    catalogue = [first, last]
    assert catalogue_bounds(catalogue) == (date(2025, 1, 15), date(2025, 1, 25))
    assert resolve_observation(catalogue, date(2025, 1, 15)) is first
    assert observations_on_day(catalogue, date(2025, 1, 20)) == ()
    with pytest.raises(LocalPreviewError, match='Aucune acquisition'):
        resolve_observation(catalogue, date(2025, 1, 20))
    with pytest.raises(LocalPreviewError, match='bornes'):
        resolve_observation(catalogue, date(2025, 1, 26))


def test_multiple_acquisitions_require_choice_and_keep_full_provenance():
    first, second = observation(25, hour=9, product='first'), observation(25, hour=10, product='second')
    with pytest.raises(LocalPreviewError, match='explicitement'):
        resolve_observation([first, second], date(2025, 1, 25))
    assert resolve_observation([first, second], date(2025, 1, 25), second) is second
    validate_chronology(first, second)  # Distinct times within the same UTC day are allowed.
    with pytest.raises(LocalPreviewError, match='journée demandée'):
        resolve_observation([first, second], date(2025, 1, 25), observation(15))
    assert '10:00:00 UTC' in observation_label(second) and 'second' in observation_label(second)
    assert 'private' not in observation_label(second)


def test_utc_day_not_system_or_source_local_day():
    source = LocalObservation(datetime(2025, 1, 26, 1, tzinfo=timezone(timedelta(hours=3))), Path('unused'), 'utc-product')
    assert acquisition_day(source) == date(2025, 1, 25)
    assert '25/01/2025 · 22:00:00 UTC' in observation_label(source)


def test_invalid_and_empty_selections_are_explicit():
    with pytest.raises(LocalPreviewError): catalogue_bounds([])
    with pytest.raises(LocalPreviewError): resolve_observation([observation(15)], None)
    assert observations_on_day([observation(15)], None) == ()
    with pytest.raises(LocalPreviewError): observations_on_day([observation(15)], '2025-01-15')
    with pytest.raises(LocalPreviewError, match='chronologique'): validate_chronology(observation(25), observation(15))
    with pytest.raises(LocalPreviewError): validate_chronology(observation(15), observation(15))


@pytest.mark.parametrize(('coverage', 'count', 'expected'), [(0, 0, 'Aucun pixel comparable'),
                         (4, 1, 'insuffisante'), (25, 1, 'exploratoire'), (57.4, 1, None)])
def test_existing_coverage_policy_is_notice_only(coverage, count, expected):
    result = SimpleNamespace(comparison=SimpleNamespace(valid_date_1=np.array([True]), valid_date_2=np.array([False])),
                             analysis=SimpleNamespace(statistics={'count': count}, comparable_coverage_percent=coverage))
    notices = observation_quality_notices(result)
    assert 'Observation finale' in notices[0]
    if expected: assert any(expected in notice for notice in notices)
    else: assert len(notices) == 1
    assert result.analysis.comparable_coverage_percent == coverage


def test_calendars_are_bounded_and_missing_day_does_not_reuse_previous_result(local_data, monkeypatch):
    from tests.test_temporal_streamlit import configured_app
    app, prepare, analyze = configured_app(local_data, monkeypatch)
    app.run(timeout=30)
    assert len(app.date_input) == 2 and not app.selectbox
    for widget in app.date_input:
        assert widget.min == date(2025, 1, 15) and widget.max == date(2025, 1, 25)
    app.date_input[0].set_value(date(2025, 1, 20)).run()
    assert app.error and any('Aucune acquisition' in message.value for message in app.error)
    assert not app.metric
    assert analyze.call_count == prepare.call_count == 1
    assert app.date_input[0].value == date(2025, 1, 20)
    app.date_input[0].set_value(date(2025, 1, 15)).run()
    assert not app.error and len(app.metric) == 4
    assert analyze.call_count == prepare.call_count == 1


def test_dates_reversed_do_not_swap_or_calculate(local_data, monkeypatch):
    from tests.test_temporal_streamlit import configured_app
    app, prepare, analyze = configured_app(local_data, monkeypatch)
    app.run(timeout=30)
    app.date_input[0].set_value(date(2025, 1, 25))
    app.date_input[1].set_value(date(2025, 1, 15)).run()
    assert app.error and 'Dates inversées' in app.error[0].value
    assert [w.value for w in app.date_input] == [date(2025, 1, 25), date(2025, 1, 15)]
    assert analyze.call_count == prepare.call_count == 1


def test_catalogue_includes_verified_dates_outside_original_akanda_pair(local_data):
    import json
    from src.services.local_preview import list_local_observations
    path = local_data/'raw/sentinel2/product-25/acquisition_manifest.json'
    manifest = json.loads(path.read_text())
    manifest['acquired_at_utc'] = '2025-02-03T09:30:00Z'
    path.write_text(json.dumps(manifest))
    catalogue = list_local_observations(local_data)
    assert len(catalogue) == 2
    assert catalogue_bounds(catalogue)[1] == date(2025, 2, 3)


# Imported here to share the existing strict synthetic acquisition fixture.
from tests.test_local_preview import local_data
