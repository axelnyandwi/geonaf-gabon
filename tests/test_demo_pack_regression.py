"""Demo pack vs full verified tiles (real data, local only; skipped when absent)."""

import json

import numpy as np
import pytest
import rasterio

from src.data.derived_subset import sha256_file, validate_subset_manifest
from src.data.preparation import clip_raster_to_aoi
from tests import demo_pack_oracle as oracle

real_data = pytest.mark.skipif(not oracle.available(), reason="Tuiles complètes ou paquet de démonstration absents.")


@real_data
def test_pack_files_match_their_manifests():
    pack = json.loads((oracle.PACK_DIR / "PACK_MANIFEST.json").read_text(encoding="utf-8"))
    for item in pack["files"]:
        path = oracle.PACK_DIR / item["path"]
        assert path.stat().st_size == item["bytes"] and sha256_file(path) == item["sha256"]
    for product in oracle.PRODUCTS:
        directory = oracle.PACK_DIR / "raw" / "sentinel2" / product
        manifest = json.loads((directory / "subset_manifest.json").read_text(encoding="utf-8"))
        validate_subset_manifest(manifest)
        assert sha256_file(directory / "source_acquisition_manifest.json") == manifest["parent"]["manifest_sha256"]
        for record in manifest["assets"].values():
            assert sha256_file(directory / record["path"]) == record["sha256"]


@real_data
def test_low_coverage_date_is_flagged_out_of_trends():
    pack = json.loads((oracle.PACK_DIR / "PACK_MANIFEST.json").read_text(encoding="utf-8"))
    uses = {item["product_id"][11:19]: item["quality"]["trend_use"] for item in pack["sentinel2"]["acquisitions"]}
    assert uses == {"20250115": "eligible", "20250120": "excluded_low_coverage", "20250125": "eligible"}


@real_data
def test_subset_pixels_and_aoi_masks_equal_full_tiles():
    for key, item in oracle.raster_checks(oracle.load_sources()).items():
        assert item["subset_equals_tile_window"], key
        assert item["aoi_clip_grid_equal"], key
        assert item["aoi_clip_pixels_equal"], key


@real_data
def test_aoi_clip_of_full_scl_tiles_keeps_every_class_value(tmp_path):
    """The clipped SCL equals a direct read of the JP2 window: same classes, same counts."""
    import geopandas as gpd
    from rasterio.mask import mask

    aoi = oracle.DATA_DIR / "aoi" / f"{oracle.AOI_ID}.geojson"
    for acquisition in oracle.load_sources()["full"][0]:
        source = acquisition.scl.path
        clipped = clip_raster_to_aoi(source, tmp_path / f"{acquisition.product_id}-scl.tif", aoi, all_touched=True)
        with rasterio.open(source) as tile, rasterio.open(clipped) as result:
            expected, transform = mask(tile, list(gpd.read_file(aoi).to_crs(tile.crs).geometry),
                                       crop=True, all_touched=True)
            values = result.read()
            assert result.driver == "GTiff" and result.dtypes[0] == tile.dtypes[0] == "uint8"
            assert result.transform == transform and result.crs == tile.crs
        assert np.array_equal(values, expected)
        assert dict(zip(*np.unique(values, return_counts=True))) == \
            dict(zip(*np.unique(expected, return_counts=True)))


@real_data
def test_scientific_results_identical_on_full_tiles_and_subsets():
    """Every NDVI date, pair, map payload, series and indicator: bit-for-bit, no tolerance used."""
    report = oracle.run()
    for key, item in report["rasters"].items():
        assert all(value for name, value in item.items() if name != "aoi_clip_shape"), key
    for name, item in report["results"].items():
        assert item["identical"], (name, item["mismatches"][:3])


def _write(path, values, driver, **options):
    from affine import Affine

    with rasterio.open(path, "w", driver=driver, width=values.shape[1], height=values.shape[0], count=1,
                       dtype=values.dtype, crs="EPSG:32632", transform=Affine(10, 0, 500000, 0, -10, 100000),
                       **options) as target:
        target.write(values, 1)
    return path


def _aoi(tmp_path):
    aoi = tmp_path / "aoi.geojson"
    aoi.write_text(json.dumps({"type": "FeatureCollection", "crs": {"type": "name", "properties": {
        "name": "urn:ogc:def:crs:EPSG::32632"}}, "features": [{"type": "Feature", "properties": {}, "geometry": {
            "type": "Polygon", "coordinates": [[[500100, 99900], [500500, 99900], [500500, 99500],
                                                [500100, 99500], [500100, 99900]]]}}]}), encoding="utf-8")
    return aoi


def _assert_lossless_clip(source, tmp_path):
    clipped = clip_raster_to_aoi(source, tmp_path / "clip.tif", _aoi(tmp_path), all_touched=True)
    with rasterio.open(source) as original, rasterio.open(clipped) as result:
        window = rasterio.windows.from_bounds(*result.bounds, transform=original.transform)
        assert result.driver == "GTiff"
        assert result.tags(ns="IMAGE_STRUCTURE").get("COMPRESSION") == "DEFLATE"
        assert (result.dtypes, result.crs, result.nodata) == (original.dtypes, original.crs, original.nodata)
        assert result.transform == original.window_transform(window)
        return result.read(1), original.read(1, window=window)


REFLECTANCE = np.random.default_rng(2).integers(0, 12000, (64, 64), dtype=np.uint16)
SCL_CLASSES = np.random.default_rng(3).integers(0, 12, (64, 64), dtype=np.uint8)


@pytest.mark.parametrize("values", [REFLECTANCE, SCL_CLASSES], ids=["reflectance", "scl"])
def test_aoi_clip_of_a_jpeg2000_source_is_lossless(tmp_path, values):
    source = _write(tmp_path / "tile.jp2", values, "JP2OpenJPEG", REVERSIBLE="YES", QUALITY=100)
    clipped, expected = _assert_lossless_clip(source, tmp_path)
    assert np.array_equal(clipped, expected)
    if values.dtype == np.uint8:
        assert set(np.unique(clipped)) <= set(range(12))


def test_aoi_clip_of_a_geotiff_keeps_float_values_and_nodata(tmp_path):
    values = np.random.default_rng(4).normal(size=(64, 64)).astype(np.float32)
    source = _write(tmp_path / "tile.tif", values, "GTiff", nodata=-9999.0)
    clipped, expected = _assert_lossless_clip(source, tmp_path)
    assert np.array_equal(clipped, expected)


