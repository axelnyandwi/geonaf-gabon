"""Contract, display-only precision and bounded-cache tests; no network."""

import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from io import BytesIO
import json
from types import SimpleNamespace

import geopandas as gpd
import numpy as np
from PIL import Image
import pytest
from rasterio.crs import CRS
from rasterio.transform import from_origin
from shapely.geometry import Polygon, box

from src.geo.temporal import RasterGrid, TemporalComparison
from src.geo.change_analysis import analyze_ndvi_changes
from src.services import temporal_map as module
from src.services.temporal_map import TemporalMapCache, prepare_temporal_map


@pytest.fixture
def result(tmp_path):
    path = tmp_path/'private-aoi.geojson'
    geometry = Polygon([(0, 0), (300, 0), (300, 300), (0, 300)],
                       holes=[[(110, 110), (190, 110), (190, 190), (110, 190)]])
    gpd.GeoDataFrame({'private': ['confidential-marker'], 'geometry': [geometry]},
                     crs='EPSG:3857').to_file(path)
    grid = RasterGrid(CRS.from_epsg(3857), (100, 100), from_origin(0, 300, 100, 100), 4, 3)
    initial = np.zeros((3, 4), dtype=np.float32)
    final = np.array([[-.1, .1, -.2, .8], [.2, .0, .4, .8], [.5, .6, .7, .8]], dtype=np.float32)
    valid_initial = np.ones((3, 4), dtype=bool); valid_initial[1, 0] = False
    valid_final = np.ones((3, 4), dtype=bool); valid_final[2, 1] = False
    comparison_mask = valid_initial & valid_final
    delta = final-initial; delta[~comparison_mask] = np.nan
    comparison = TemporalComparison(grid, initial, final, valid_initial, valid_final, comparison_mask, delta)
    # A float32 threshold makes the two boundary values exactly equal.
    threshold = float(np.float32(.1))
    analysis = analyze_ndvi_changes(comparison, threshold=threshold, aoi_path=path)
    def acquisition(day):
        return SimpleNamespace(product_id=f'S2-public-{day}',
                               acquired_at_utc=datetime(2025, 1, day, tzinfo=timezone.utc),
                               asset_paths={'secret': str(tmp_path)},
                               calibration=SimpleNamespace(red_offset=-1000, nir_offset=-1000,
                                                           quantification_value=10000))
    return SimpleNamespace(common_grid=grid, comparison=comparison, analysis=analysis,
                           threshold=threshold, scl_policy='usable_land', aoi_path=path,
                           earlier=acquisition(15), later=acquisition(25))


def decode(uri):
    return np.asarray(Image.open(BytesIO(base64.b64decode(uri.split(',', 1)[1]))))


def test_contract_shared_grid_masks_thresholds_and_source_identity(result):
    arrays = [result.comparison.ndvi_date_1, result.comparison.ndvi_date_2,
              result.analysis.delta_ndvi, result.analysis.change_class_map,
              result.analysis.comparable_mask]
    before = [a.tobytes() for a in arrays]
    indicators = json.dumps({'stats': result.analysis.statistics, 'areas': result.analysis.areas_m2,
                             'coverage': result.analysis.comparable_coverage_percent}, sort_keys=True)
    payload = prepare_temporal_map(result)
    assert payload['schema_version'] == 'geonaf.temporal-map/v1'
    assert payload['grid']['width'] == 4 and payload['grid']['height'] == 3
    assert [layer['id'] for layer in payload['layers']] == ['ndvi_initial', 'ndvi_final', 'delta', 'classes']
    for layer in payload['layers']:
        assert decode(layer['image']).shape == (3, 4, 4)
    initial, final, delta, classes = [decode(layer['image']) for layer in payload['layers']]
    assert initial[1, 0].tolist() == list(module.MASKED_RGBA)
    assert final[1, 0].tolist() != list(module.MASKED_RGBA)
    assert delta[1, 0].tolist() == list(module.MASKED_RGBA)
    assert delta[2, 1].tolist() == list(module.MASKED_RGBA)
    assert np.all(classes[:, 3, 3] == 0)  # exterior, never a stable class
    for col in (0, 1):  # exact +/- threshold belongs to stable
        assert classes[0, col].tolist() == [*bytes.fromhex(module.CLASS_COLORS[2][1:]), 255]
    assert decode(payload['masks']['comparable'])[1, 0] == 1
    assert decode(payload['masks']['comparable'])[0, 3] == 0
    assert before == [a.tobytes() for a in arrays]
    assert indicators == json.dumps({'stats': result.analysis.statistics, 'areas': result.analysis.areas_m2,
                                    'coverage': result.analysis.comparable_coverage_percent}, sort_keys=True)
    category = decode(payload['masks']['comparable'])
    for code, color in module.CLASS_COLORS.items():
        selected = (result.analysis.change_class_map == code) & (category == 2)
        assert np.all(classes[selected] == np.array([*bytes.fromhex(color[1:]), 255]))
    assert payload['provenance']['initial']['calibration']['red_offset'] == -1000
    assert payload['layers'][2]['range'] == [-2, 2]
    assert payload['layers'][0]['legend'] == payload['layers'][1]['legend']
    assert payload['result_id'] == prepare_temporal_map(result)['result_id']


def test_no_private_metadata_or_nonfinite_json(result):
    public = json.dumps(prepare_temporal_map(result), allow_nan=False)
    assert str(result.aoi_path.parent) not in public
    assert 'confidential-marker' not in public and 'asset_paths' not in public
    assert 'private-aoi' not in public


def test_continuous_legend_uses_exact_raster_lookup_table(result):
    from matplotlib import colormaps
    payload = prepare_temporal_map(result)
    expected = colormaps['RdYlGn'](np.linspace(0, 1, 256), bytes=True)[None, :, :]
    for layer in payload['layers'][:3]:
        np.testing.assert_array_equal(decode(layer['legend_image']), expected)
    assert 'legend_image' not in payload['layers'][3]


@pytest.mark.parametrize('size', [1, 2, 3])
def test_size_is_transmitted_and_geographic_corners_are_shared(result, size):
    payload = TemporalMapCache().prepare(result, scientific_key=('revision', 1), max_size=size)
    grid = payload['grid']
    assert max(grid['width'], grid['height']) <= size
    nw, ne, se, sw = grid['coordinates']
    assert nw[0] < ne[0] and nw[1] > sw[1]
    assert ne[1] == pytest.approx(nw[1], abs=1e-12)
    assert se[1] == pytest.approx(sw[1], abs=1e-12)
    for layer in payload['layers']:
        assert decode(layer['image']).shape[:2] == (grid['height'], grid['width'])


def test_utm_georeferencing(result, tmp_path):
    result.common_grid = RasterGrid(CRS.from_epsg(32632), (100, 100),
                                    from_origin(537000, 63000, 100, 100), 4, 3)
    gpd.GeoDataFrame(geometry=[box(537000, 62700, 537400, 63000)], crs='EPSG:32632').to_file(tmp_path/'utm.geojson')
    result.aoi_path = tmp_path/'utm.geojson'
    payload = prepare_temporal_map(result)
    for lon, lat in payload['grid']['coordinates']:
        assert 9 < lon < 10 and 0 < lat < 1
    # Returned Mercator transform reproduces the exact image corners.
    from pyproj import Transformer
    from affine import Affine
    grid = payload['grid']; transform = Affine(*grid['transform'])
    project = Transformer.from_crs(3857, 4326, always_xy=True)
    expected = project.transform(*(transform @ (grid['width'], grid['height'])))
    np.testing.assert_allclose(grid['coordinates'][2], expected, rtol=0, atol=1e-12)


@pytest.mark.parametrize('size', [True, 0, -1, 2049, 2.5])
def test_invalid_display_sizes(result, size):
    with pytest.raises(ValueError): prepare_temporal_map(result, max_size=size)


def test_memory_guard_precedes_aoi_allocation(result, monkeypatch):
    monkeypatch.setattr(module, 'MAX_WORKING_BYTES', 1)
    monkeypatch.setattr(module, '_rasterize_aoi_pixel_mask', lambda *args: pytest.fail('allocation'))
    with pytest.raises(ValueError, match='mémoire'): prepare_temporal_map(result)


def test_payload_budget(result, monkeypatch):
    monkeypatch.setattr(module, 'MAX_PAYLOAD_BYTES', 1)
    with pytest.raises(ValueError, match='Charge utile'): prepare_temporal_map(result)


def test_cache_limits_lru_invalidation_and_isolation(result, monkeypatch):
    calls = []
    original = module.prepare_temporal_map
    def traced(*args, **kwargs):
        calls.append(kwargs['max_size']); return original(*args, **kwargs)
    monkeypatch.setattr(module, 'prepare_temporal_map', traced)
    cache = TemporalMapCache(max_entries=2)
    first = cache.prepare(result, scientific_key='a', max_size=2)
    first['layers'][0]['legend'].clear()
    assert cache.prepare(result, scientific_key='a', max_size=2)['layers'][0]['legend']
    cache.prepare(result, scientific_key='b', max_size=2)
    cache.prepare(result, scientific_key='a', max_size=2)  # a is most recent
    cache.prepare(result, scientific_key='c', max_size=2)  # b evicted
    cache.prepare(result, scientific_key='b', max_size=2)
    assert len(calls) == 4 and cache.info()['entries'] == 2
    cache.prepare(result, scientific_key='b', max_size=1)
    assert calls[-1] == 1 and len(calls) == 5
    tiny = TemporalMapCache(max_bytes=1)
    tiny.prepare(result, scientific_key='a')
    assert tiny.info()['bytes'] == 0 and tiny.info()['entries'] == 0
    size = len(module._json_bytes(original(result)))+32
    bounded = TemporalMapCache(max_bytes=size+10)
    for key in ('a', 'b', 'c'):
        bounded.prepare(result, scientific_key=key)
        assert bounded.info()['bytes'] <= size+10
    assert bounded.info()['entries'] == 1
    bounded.clear(); assert bounded.info()['bytes'] == bounded.info()['entries'] == 0


def test_concurrent_cache_prepares_once_and_returns_isolated_objects(result):
    cache = TemporalMapCache()
    with ThreadPoolExecutor(max_workers=4) as pool:
        payloads = list(pool.map(lambda _: cache.prepare(result, scientific_key='same'), range(8)))
    assert cache.info()['misses'] == 1 and cache.info()['hits'] == 7
    payloads[0]['layers'].clear()
    assert all(len(p['layers']) == 4 for p in payloads[1:])


@pytest.mark.parametrize('kwargs', [{'max_entries': 5}, {'max_entries': True}, {'max_bytes': 0},
                                    {'max_bytes': 65*1024**2}])
def test_invalid_cache_budgets(kwargs):
    with pytest.raises(ValueError): TemporalMapCache(**kwargs)


def test_failure_not_cached(result):
    cache = TemporalMapCache()
    with pytest.raises(ValueError): cache.prepare(result, scientific_key='same', max_size=0)
    assert cache.info()['entries'] == 0
    with pytest.raises(ValueError): cache.prepare(result, scientific_key=None)


def test_bad_policy_threshold_shape_and_polar_grid_fail_closed(result):
    result.threshold += .1
    with pytest.raises(ValueError, match='Seuil'): prepare_temporal_map(result)
    result.threshold = result.analysis.threshold
    result.scl_policy = 'land_surface'
    with pytest.raises(ValueError, match='usable_land'): prepare_temporal_map(result)
    result.scl_policy = 'usable_land'
    result.common_grid = RasterGrid(CRS.from_epsg(4326), (1, 1), from_origin(0, 90, 1, 1), 4, 3)
    with pytest.raises(ValueError, match='Mercator'): prepare_temporal_map(result)


def test_hole_partial_cells_and_aoi_revision_invalidate_without_source_changes(result):
    cache = TemporalMapCache()
    before = cache.prepare(result, scientific_key='same')
    hole = Polygon([(25, 0), (275, 0), (275, 300), (25, 300)],
                   holes=[[(90, 90), (210, 90), (210, 210), (90, 210)]])
    gpd.GeoDataFrame(geometry=[hole], crs='EPSG:3857').to_file(result.aoi_path)
    after = cache.prepare(result, scientific_key='same')
    mask = decode(after['masks']['initial'])
    assert mask[1, 1] == 0  # full cell in the hole
    assert mask[0, 0] == 2 and mask[0, 2] == 2  # partially covered edge cells
    assert after['result_id'] != before['result_id']
    assert cache.info()['misses'] == 2


def test_final_analysis_mask_not_raw_comparison_controls_delta(result):
    result.comparison.ndvi_date_2[0, 2] = 1.1
    result.analysis = analyze_ndvi_changes(result.comparison, threshold=result.threshold,
                                         aoi_path=result.aoi_path)
    payload = prepare_temporal_map(result)
    assert result.comparison.comparison_mask[0, 2]
    assert not result.analysis.comparable_mask[0, 2]
    assert decode(payload['masks']['comparable'])[0, 2] == 1
    assert decode(payload['layers'][2]['image'])[0, 2].tolist() == list(module.MASKED_RGBA)
    assert payload['provenance']['excluded_out_of_range_pixels'] == 1


def test_bad_shape_and_antimeridian(result):
    result.common_grid = RasterGrid(CRS.from_epsg(3857), (100, 100), from_origin(0, 300, 100, 100), 5, 3)
    with pytest.raises(ValueError, match='Tableaux'): prepare_temporal_map(result)
    result.common_grid = RasterGrid(CRS.from_epsg(4326), (90, 1), from_origin(-179, 3, 90, 1), 4, 3)
    with pytest.raises(ValueError, match='antiméridien'): prepare_temporal_map(result)


def test_metadata_changes_invalidate_cache(result):
    cache = TemporalMapCache()
    cache.prepare(result, scientific_key='same')
    # Same geometry/arrays, but new public acquisition identity.
    result.earlier.product_id = 'S2-revision'
    changed = cache.prepare(result, scientific_key='same')
    assert changed['provenance']['initial']['product_id'] == 'S2-revision'
    assert cache.info()['misses'] == 2
