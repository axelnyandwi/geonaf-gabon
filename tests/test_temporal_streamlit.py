"""Integration tests of the real app with synthetic, verified acquisitions."""

from pathlib import Path
from unittest.mock import Mock
import json
from datetime import date

from streamlit.testing.v1 import AppTest

from tests.test_local_preview import local_data, submit_dates  # Reuse the verified acquisition fixture.
from src.services.local_preview import analyze_local_akanda, list_local_observations, local_preview_key


def configured_app(local_data, monkeypatch):
    import src.config
    import src.components.temporal_map as adapter
    import src.services.temporal_map as layers
    import src.services.local_preview as preview

    monkeypatch.setattr(src.config, 'DATA_DIR', local_data)
    adapter._renderer.clear()
    adapter._payload_cache.clear()
    prepare = Mock(wraps=layers.prepare_temporal_map)
    analyze = Mock(wraps=preview.analyze_local_akanda)
    monkeypatch.setattr(layers, 'prepare_temporal_map', prepare)
    monkeypatch.setattr(preview, 'analyze_local_akanda', analyze)
    return AppTest.from_file(str(Path(__file__).resolve().parents[1]/'app.py')), prepare, analyze


def test_default_comparison_reuses_result_and_bounded_payload(local_data, monkeypatch):
    app, prepare, analyze = configured_app(local_data, monkeypatch)
    app.run(timeout=30)
    assert not app.exception and not app.error
    assert app.checkbox(key='temporal_comparison').value
    assert len(app.metric) == 4 and len(app.table) == 2
    submit_dates(app).run()
    app.run()
    assert prepare.call_count == analyze.call_count == 1
    import src.components.temporal_map as adapter
    info = adapter._payload_cache().info()
    assert info['entries'] == 1 and info['bytes'] <= 64*1024**2
    assert info['hits'] == 2


def test_size_and_public_contract_and_response_isolation(local_data):
    from src.components.temporal_map import cached_temporal_payload, _payload_cache
    _payload_cache.clear()
    pair = list_local_observations(local_data)
    result = analyze_local_akanda(*pair, local_data)
    key = local_preview_key(*pair, local_data)
    payload = cached_temporal_payload(key, result, max_size=1)
    assert payload['schema_version'] == 'geonaf.temporal-map/v1'
    assert max(payload['grid']['width'], payload['grid']['height']) == 1
    payload['layers'].clear()
    assert len(cached_temporal_payload(key, result, max_size=1)['layers']) == 4


def test_invalid_dates_hide_previous_comparison_and_recovery_is_cached(local_data, monkeypatch):
    app, prepare, analyze = configured_app(local_data, monkeypatch)
    app.run(timeout=30)
    app.date_input[0].set_value(date(2025, 1, 25))
    submit_dates(app).run()
    assert not app.exception and app.error and not app.metric
    assert 'chronologique' in app.error[0].value
    assert prepare.call_count == 1
    app.date_input[0].set_value(date(2025, 1, 15))
    submit_dates(app).run()
    assert not app.exception and not app.error
    assert prepare.call_count == 1


def test_aoi_revision_invalidates_science_and_temporal_cache(local_data, monkeypatch):
    app, prepare, analyze = configured_app(local_data, monkeypatch)
    app.run(timeout=30)
    path = local_data/'aoi/akanda.geojson'
    path.write_bytes(path.read_bytes()+b'\n')
    app.run()
    assert not app.exception and not app.error
    assert prepare.call_count == analyze.call_count == 2


def test_renderer_failure_uses_png_and_retains_indicators(local_data, monkeypatch):
    app, prepare, analyze = configured_app(local_data, monkeypatch)
    import src.components.temporal_map as adapter
    import src.components.preview_cache as images
    png = Mock(wraps=images.cached_map_png)
    monkeypatch.setattr(images, 'cached_map_png', png)
    monkeypatch.setattr(adapter, 'show_temporal_map', lambda payload: False)
    app.run(timeout=30)
    assert not app.exception and not app.error
    assert len(app.metric) == 4 and png.call_count == 1
    assert any('Carte interactive indisponible' in item.value for item in app.warning)
    submit_dates(app).run()
    assert analyze.call_count == prepare.call_count == 1


def test_preparation_exception_is_safe_and_legacy_view_remains_available(local_data, monkeypatch):
    app, prepare, analyze = configured_app(local_data, monkeypatch)
    import src.components.temporal_map as adapter
    def failure(*args, **kwargs):
        raise RuntimeError('private-source-path-placeholder')
    monkeypatch.setattr(adapter, 'cached_temporal_payload', failure)
    app.run(timeout=30)
    assert not app.exception and not app.error
    assert all('private-source' not in item.value for item in app.warning)
    app.checkbox(key='temporal_comparison').uncheck().run()
    assert app.checkbox(key='interactive_map').value
    assert len(app.metric) == 4
    assert analyze.call_count == 1


def test_different_valid_acquisition_invalidates_component_and_payload(local_data, monkeypatch):
    """A third synthetic acquisition tests valid date changes without real writes."""
    import shutil
    from datetime import datetime, timezone
    from src.data.acquisition_manifest import build_acquisition_manifest, write_acquisition_manifest
    import src.components.temporal_map as adapter

    source = local_data/'raw/sentinel2/product-25'
    destination = source.with_name('product-25-later')
    shutil.copytree(source, destination, ignore=shutil.ignore_patterns('acquisition_manifest.json'))
    product_id = 'S2B_TEST_25_LATER'
    xml = destination/'metadata.xml'
    xml.write_text(xml.read_text().replace('S2B_TEST_25', product_id), encoding='utf-8')
    paths = {role: destination/name for role, name in (
        ('b04', 'b04.tif'), ('b08', 'b08.tif'), ('scl', 'scl.tif'), ('calibration_xml', 'metadata.xml'))}
    previous = json.loads((source/'acquisition_manifest.json').read_text())
    manifest = build_acquisition_manifest(
        product={'id': product_id, 'collection': 'sentinel-2-l2a',
                 'properties': {'datetime': '2025-01-25T10:30:00Z'},
                 'assets': {role: {'href': f's3://test/{role}'} for role in paths}},
        asset_paths=paths, asset_ids={role: role for role in paths},
        stac_source='https://example.org/stac', ingested_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        product_directory=destination, integrity=previous['integrity'])
    write_acquisition_manifest(manifest, destination/'acquisition_manifest.json')
    app, prepare, analyze = configured_app(local_data, monkeypatch)
    show = Mock(return_value=True)
    monkeypatch.setattr(adapter, 'show_temporal_map', show)
    app.run(timeout=30)
    assert not show.called  # A calendar day never silently picks one of two products.
    picker = lambda: next(box for box in app.selectbox if box.label.startswith('Acquisition de la date'))
    picker().select_index(1)
    submit_dates(app).run()
    first = show.call_args.args[0]
    assert first['provenance']['final']['product_id'] == product_id
    picker().select_index(0)
    submit_dates(app).run()
    assert not app.exception and not app.error
    second = show.call_args.args[0]
    assert second['provenance']['final']['product_id'] == 'S2B_TEST_25'
    assert first['result_id'] != second['result_id']
    assert prepare.call_count == analyze.call_count == 2
