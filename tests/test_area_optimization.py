"""Regression tests for conservative surface blocks and bounded reuse."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import numpy as np
import pytest
from affine import Affine
from rasterio.crs import CRS
from shapely.geometry import box, Polygon, MultiPolygon
from shapely import transform as geometry_transform
from pyproj import Transformer

from src.geo import change_analysis as area
from src.geo.temporal import RasterGrid
from area_weights_oracle import reference_pixel_area_chunks, reference_cell_area_sums


def grid(width=32, height=24, crs=6933, affine=None):
    return RasterGrid(CRS.from_epsg(crs), (10., 10.),
                      affine or Affine(10, 0, 0, 0, -10, 240), width, height)


def oracle_weights(g, geometry):
    result = np.zeros((g.height, g.width))
    for start, end, values in reference_pixel_area_chunks(g, geometry):
        result[start:end] = values
    return result


def test_weights_reused_without_exposing_cached_memory():
    area.clear_area_weight_cache()
    g = grid()
    aoi = area.EqualAreaAOI(box(1, 1, 319, 239), 318*238)
    expected = area.prepare_pixel_area_weights(g, aoi)
    first = area.area_weight_cache_info()
    expected[:] = -1
    reused = area.prepare_pixel_area_weights(g, aoi)
    assert np.all(reused >= 0)
    assert not np.shares_memory(expected, reused)
    assert first['misses'] == 1
    assert area.area_weight_cache_info()['hits'] == 1
    np.testing.assert_array_equal(reused, oracle_weights(g, aoi.geometry))


@pytest.fixture(autouse=True)
def empty_cache():
    area.clear_area_weight_cache()
    yield
    area.clear_area_weight_cache()


@pytest.mark.parametrize("geometry", [
    None, box(0, 0, 320, 240), box(3.25, 4.75, 315.5, 237.25),
    box(-100, 30, 210, 500), box(320, 0, 400, 240),
    Polygon([(0, 0), (320, 0), (320, 240), (0, 240)],
            holes=[[(91, 81), (191, 81), (191, 181), (91, 181)]]),
    Polygon([(1, 1), (319, 1), (319, 61), (81, 61), (81, 239), (1, 239)]),
    MultiPolygon([box(1, 1, 81, 91), box(201, 141, 319, 239)]),
    box(99.9999999, 0, 100.0000001, 240),
], ids=["no-aoi", "aligned", "partial", "outside-grid", "edge-contact",
        "hole", "concave", "multipart", "sliver"])
def test_weights_and_indicators_match_frozen_oracle(geometry):
    g = grid()
    expected = oracle_weights(g, geometry)
    mask = np.random.default_rng(19).random(expected.shape) > .35
    classes = np.random.default_rng(23).integers(1, 4, expected.shape, dtype=np.uint8)
    classes[~mask] = 0
    actual = area._compute_area_weights(g, geometry)
    np.testing.assert_allclose(actual, expected, rtol=1e-12, atol=1e-8)
    np.testing.assert_array_equal(actual > 0, expected > 0)
    old = reference_cell_area_sums(g, mask, classes, geometry)
    new = area._cell_area_sums(g, mask, classes, geometry)
    np.testing.assert_allclose(new[:2], old[:2], rtol=1e-12, atol=1e-8)
    np.testing.assert_allclose(list(new[2].values()), list(old[2].values()),
                               rtol=1e-12, atol=1e-8)
    denominator = geometry.area if geometry is not None else old[0]
    assert abs(new[1]/denominator*100-old[1]/denominator*100) <= 1e-9
    assert sum(new[2].values()) == pytest.approx(new[1], rel=1e-12, abs=1e-8)


@pytest.mark.parametrize("crs,affine", [
    (32632, Affine(10, 0, 500000, 0, -10, 1003000)),
    (32632, Affine(10, 2, 500000, 1, -10, 1003000)),
    (4326, Affine(.02, 0, 9, 0, -.02, 65)),
    (4326, Affine(.02, .005, 9, .003, -.02, 65)),
])
def test_projected_rotated_cells_and_holes_match_oracle(crs, affine):
    g = grid(crs=crs, affine=affine)
    source = Polygon([affine @ p for p in [(1.2, 1.7), (30.7, 1.7), (30.7, 22.3), (1.2, 22.3)]],
                     holes=[[affine @ p for p in [(9.3, 8.2), (19.3, 8.2), (19.3, 16.4), (9.3, 16.4)]]])
    transformer = Transformer.from_crs(g.crs, area.EQUAL_AREA_CRS, always_xy=True)
    geometry = geometry_transform(source, transformer.transform, interleaved=False)
    actual = area._compute_area_weights(g, geometry)
    expected = oracle_weights(g, geometry)
    np.testing.assert_allclose(actual, expected, rtol=1e-12, atol=1e-8)
    np.testing.assert_array_equal(actual > 0, expected > 0)


def test_conservative_envelope_uses_all_nonlinearly_projected_vertices(monkeypatch):
    class CurvedProjection:
        def transform(self, x, y):
            return x, y + 40*np.sin(np.asarray(x)*np.pi/320)
    monkeypatch.setattr(area.Transformer, "from_crs", lambda *a, **k: CurvedProjection())
    g = grid()
    geometry = box(-1, -1, 321, 241)
    expected = oracle_weights(g, geometry)
    actual = area._compute_area_weights(g, geometry)
    np.testing.assert_allclose(actual, expected, rtol=1e-12, atol=1e-8)
    assert np.any(expected[0] < 100)


def test_interior_tiles_avoid_individual_polygons(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("An interior tile must not construct cell polygons")
    monkeypatch.setattr(area, "polygons", forbidden)
    values = area._compute_area_weights(grid(520, 300), box(-1, -4000, 6000, 1000))
    np.testing.assert_array_equal(values, np.full((300, 520), 100.))


def test_tile_bounds_and_very_wide_grid(monkeypatch):
    original = area._projected_tile
    shapes = []
    def measured(g, transformer, r, re, c, ce):
        shapes.append((re-r, ce-c))
        assert (re-r)*(ce-c) <= area.MAX_AREA_CELLS_PER_CHUNK
        return original(g, transformer, r, re, c, ce)
    monkeypatch.setattr(area, "_projected_tile", measured)
    area._compute_area_weights(grid(520, 300), None)
    assert (256, 256) in shapes
    shapes.clear()
    values = area._compute_area_weights(grid(100_001, 1), None)
    assert max(w for h, w in shapes) == 256
    assert values.shape == (1, 100_001)
    monkeypatch.setattr(area, "MAX_AREA_CELLS_PER_CHUNK", 100)
    area._compute_area_weights(grid(), None)


@pytest.mark.parametrize("change", ["affine", "width", "height", "crs", "resolution",
                                   "geometry", "target", "version"])
def test_cache_invalidates_on_every_geometric_input(change, monkeypatch):
    g, geometry = grid(), box(1, 1, 319, 239)
    area.prepare_pixel_area_weights(g, area.EqualAreaAOI(geometry, geometry.area))
    changes = {"affine": {"transform": Affine(10, .1, 0, 0, -10, 240)},
               "width": {"width": 33}, "height": {"height": 25},
               "crs": {"crs": CRS.from_epsg(3857)}, "resolution": {"resolution": (11., 10.)}}
    if change in changes:
        g = replace(g, **changes[change])
    elif change == "geometry":
        geometry = box(1.01, 1, 319, 239)
    elif change == "target":
        monkeypatch.setattr(area, "EQUAL_AREA_CRS", "EPSG:3857")
    else:
        monkeypatch.setattr(area, "_AREA_ALGORITHM_VERSION", "test-next-version")
    area.prepare_pixel_area_weights(g, area.EqualAreaAOI(geometry, geometry.area))
    assert area.area_weight_cache_info()['misses'] == 2
    assert area.area_weight_cache_info()['hits'] == 0


def test_lru_entry_limit_promotes_recently_used_weights():
    g = grid()
    aois = [area.EqualAreaAOI(box(i, 1, 319, 239), (319-i)*238) for i in range(5)]
    for aoi in aois[:4]:
        area.prepare_pixel_area_weights(g, aoi)
    area.prepare_pixel_area_weights(g, aois[0])
    area.prepare_pixel_area_weights(g, aois[4])
    assert area.area_weight_cache_info()['entries'] == 4
    misses = area.area_weight_cache_info()['misses']
    area.prepare_pixel_area_weights(g, aois[0])
    assert area.area_weight_cache_info()['misses'] == misses
    area.prepare_pixel_area_weights(g, aois[1])
    assert area.area_weight_cache_info()['misses'] == misses+1
    assert area.area_weight_cache_info()['bytes'] <= 64*1024*1024


def test_byte_limit_eviction_and_oversized_streaming(monkeypatch):
    g, geometry = grid(), box(1, 1, 319, 239)
    size = g.width*g.height*8
    monkeypatch.setattr(area, "AREA_CACHE_MAX_BYTES", 2*size)
    for offset in (1, 2, 3):
        geom = box(offset, 1, 319, 239)
        area.prepare_pixel_area_weights(g, area.EqualAreaAOI(geom, geom.area))
    assert area.area_weight_cache_info()['bytes'] == 2*size
    area.clear_area_weight_cache()
    monkeypatch.setattr(area, "AREA_CACHE_MAX_BYTES", size-1)
    mask = np.ones((g.height, g.width), bool)
    classes = np.full(mask.shape, 2, np.uint8)
    expected = reference_cell_area_sums(g, mask, classes, geometry)
    actual = area._cell_area_sums(g, mask, classes, geometry)
    assert actual == expected
    assert area.area_weight_cache_info()['entries'] == 0


def test_concurrent_requests_build_once_and_return_independent_arrays(monkeypatch):
    from threading import Barrier
    barrier = Barrier(8)
    original = area._compute_area_weights
    calls = []
    def measured(*args):
        calls.append(1)
        return original(*args)
    monkeypatch.setattr(area, "_compute_area_weights", measured)
    g = grid()
    geometry = box(1, 1, 319, 239)
    aoi = area.EqualAreaAOI(geometry, geometry.area)
    def request(i):
        barrier.wait(timeout=10)
        result = area.prepare_pixel_area_weights(g, aoi)
        result[0, 0] = -i-1
        return result
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(request, range(8)))
    assert len(calls) == 1
    assert area.area_weight_cache_info()['misses'] == 1
    assert area.area_weight_cache_info()['hits'] == 7
    assert [r[0, 0] for r in results] == [-i-1 for i in range(8)]
    assert area.prepare_pixel_area_weights(g, aoi)[0, 0] >= 0


def test_failed_build_is_not_published_and_clear_resets_counters(monkeypatch):
    g = grid()
    aoi = area.EqualAreaAOI(box(0, 0, 320, 240), 320*240)
    def failed(*args):
        raise ValueError("test projection failure")
    with monkeypatch.context() as patcher:
        patcher.setattr(area, "_compute_area_weights", failed)
        with pytest.raises(ValueError, match="projection failure"):
            area.prepare_pixel_area_weights(g, aoi)
    assert area.area_weight_cache_info()['entries'] == 0
    area.prepare_pixel_area_weights(g, aoi)
    assert area.area_weight_cache_info()['misses'] == 2
    area.clear_area_weight_cache()
    assert area.area_weight_cache_info() == dict(entries=0, bytes=0, hits=0, misses=0)


def test_streamed_weights_and_reductions_are_bounded_for_very_wide_grids(monkeypatch):
    g = grid(100_001, 1)
    monkeypatch.setattr(area, "AREA_CACHE_MAX_BYTES", 1)
    for selection, values in area._pixel_area_chunks(g):
        assert values.size <= 100_000
        assert values.shape == (1, selection[1].stop-selection[1].start)
        np.testing.assert_array_equal(values, np.full(values.shape, 100.))
    mask = np.ones((1, g.width), bool)
    classes = np.full(mask.shape, 2, np.uint8)
    actual = area._cell_area_sums(g, mask, classes)
    assert actual == (10_000_100., 10_000_100.,
                      dict(decrease=0., stable=10_000_100., increase=0.))
    geometry = box(-1, -1, 1_000_011, 241)
    aoi = area.EqualAreaAOI(geometry, geometry.area)
    public_weights = np.full(mask.shape, 100.)
    direct = area.measure_mask_coverage(mask, g, aoi)
    prepared = area.measure_mask_coverage(mask, g, aoi, pixel_area_weights=public_weights)
    assert direct == prepared


def test_holes_and_partial_boundaries_across_tile_seams():
    g = grid(520, 300)
    geometry = Polygon([(0.5,-2759.5),(5199.5,-2759.5),(5199.5,239.5),(0.5,239.5)],
                       holes=[[(2501,-2381),(2621,-2381),(2621,-2261),(2501,-2261)]])
    expected = oracle_weights(g, geometry)
    actual = area._compute_area_weights(g, geometry)
    np.testing.assert_allclose(actual, expected, rtol=1e-12, atol=1e-8)
    np.testing.assert_array_equal(actual > 0, expected > 0)


def test_aoi_file_content_invalidates_cache_even_with_same_size_and_mtime(tmp_path):
    import json
    import os
    from shapely.geometry import mapping
    path = tmp_path / "aoi.geojson"
    def content(left):
        return json.dumps({"type":"FeatureCollection",
                           "crs":{"type":"name","properties":{"name":"EPSG:6933"}},
                           "features":[{"type":"Feature","properties":{},
                                        "geometry":mapping(box(left,1,319,239))}]})
    first, second = content(1.), content(2.)
    assert len(first.encode()) == len(second.encode())
    path.write_text(first, encoding="utf-8")
    stat = path.stat()
    g = grid()
    mask = np.ones((g.height,g.width),bool)
    before = area.measure_mask_coverage(mask,g,path)
    path.write_text(second, encoding="utf-8")
    os.utime(path,ns=(stat.st_atime_ns,stat.st_mtime_ns))
    assert path.stat().st_size == stat.st_size
    assert path.stat().st_mtime_ns == stat.st_mtime_ns
    after = area.measure_mask_coverage(mask,g,path)
    assert before.area_m2-after.area_m2 == pytest.approx(238.)
    assert area.area_weight_cache_info()['misses'] == 2


def test_different_concurrent_geometries_do_not_share_weights_or_exceed_cache():
    g = grid()
    geometries = [box(i+1,1,319,239) for i in range(8)]
    def request(geometry):
        aoi = area.EqualAreaAOI(geometry,geometry.area)
        values = area.prepare_pixel_area_weights(g,aoi)
        return float(values.sum())
    with ThreadPoolExecutor(max_workers=8) as pool:
        values = list(pool.map(request,geometries))
    assert values == [geometry.area for geometry in geometries]
    info = area.area_weight_cache_info()
    assert info['entries'] == 4
    assert info['bytes'] <= 64*1024*1024
    assert info['misses'] == 8


def test_clear_during_build_cannot_publish_a_stale_entry(monkeypatch):
    from threading import Event
    entered,release,cleared = Event(),Event(),Event()
    original = area._compute_area_weights
    def delayed(*args):
        entered.set()
        assert release.wait(timeout=10)
        return original(*args)
    monkeypatch.setattr(area,"_compute_area_weights",delayed)
    geometry = box(1,1,319,239)
    aoi = area.EqualAreaAOI(geometry,geometry.area)
    def clear():
        area.clear_area_weight_cache()
        cleared.set()
    with ThreadPoolExecutor(max_workers=2) as pool:
        build = pool.submit(area.prepare_pixel_area_weights,grid(),aoi)
        assert entered.wait(timeout=10)
        clearing = pool.submit(clear)
        assert not cleared.wait(timeout=.02)
        release.set()
        np.testing.assert_array_equal(build.result(timeout=10),oracle_weights(grid(),geometry))
        clearing.result(timeout=10)
    assert area.area_weight_cache_info() == dict(entries=0,bytes=0,hits=0,misses=0)


def test_cached_geometry_weights_support_different_masks_and_classes():
    g,geometry = grid(),box(1,1,319,239)
    first_mask = np.zeros((g.height,g.width),bool)
    first_mask[:, :16] = True
    second_mask = ~first_mask
    first_classes = np.where(first_mask,1,0).astype(np.uint8)
    second_classes = np.where(second_mask,3,0).astype(np.uint8)
    for mask,classes in ((first_mask,first_classes),(second_mask,second_classes)):
        expected = reference_cell_area_sums(g,mask,classes,geometry)
        actual = area._cell_area_sums(g,mask,classes,geometry)
        assert actual == expected
    info = area.area_weight_cache_info()
    assert info['misses'] == 1
    assert info['hits'] == 1
