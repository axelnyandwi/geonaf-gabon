"""Native-grid subsets: alignment, lossless copy and lineage validation (synthetic rasters)."""

from datetime import datetime, timezone

import numpy as np
import pytest
import rasterio
from affine import Affine

from src.data.derived_subset import (
    aligned_subset_bounds, build_subset_manifest, integer_window, validate_subset_manifest,
    write_raster_subset,
)

TILE = Affine(10.0, 0.0, 499980.0, 0.0, -10.0, 100020.0)


def _raster(path, values, transform=TILE, driver="GTiff", **options):
    with rasterio.open(path, "w", driver=driver, width=values.shape[1], height=values.shape[0], count=1,
                       dtype=values.dtype, crs="EPSG:32632", transform=transform, **options) as target:
        target.write(values, 1)
    return path


def test_bounds_are_snapped_outward_to_20m_and_clipped_to_tile():
    bounds = aligned_subset_bounds((500123.0, 99001.0, 500456.0, 99555.0), TILE, (100, 100), margin_m=15)
    assert bounds == (500100.0, 99020.0, 500480.0, 99580.0)  # Bottom limited by the 100-pixel tile.
    assert all((value - 499980.0) % 20 == 0 for value in bounds[::2])
    assert all((100020.0 - value) % 20 == 0 for value in bounds[1::2])
    assert aligned_subset_bounds((499000.0, 0.0, 600000.0, 200000.0), TILE, (100, 100), margin_m=0) == \
        (499980.0, 99020.0, 500980.0, 100020.0)


def test_bounds_outside_tile_are_rejected():
    with pytest.raises(ValueError):
        aligned_subset_bounds((700000.0, 0.0, 700100.0, 100.0), TILE, (100, 100), margin_m=0)


def test_integer_window_refuses_fractional_offsets():
    window = integer_window(TILE, (500000.0, 99800.0, 500100.0, 100000.0))
    assert (window.col_off, window.row_off, window.width, window.height) == (2, 2, 10, 20)
    with pytest.raises(ValueError, match="non alignée"):
        integer_window(TILE, (500005.0, 99800.0, 500100.0, 100000.0))


@pytest.mark.parametrize("driver,options", [("GTiff", {}), ("JP2OpenJPEG", {"REVERSIBLE": "YES", "QUALITY": 100})])
def test_subset_copies_pixels_exactly(tmp_path, driver, options):
    rng = np.random.default_rng(1)
    values = rng.integers(0, 12000, (60, 60), dtype=np.uint16)
    source = _raster(tmp_path / f"tile.{'jp2' if driver != 'GTiff' else 'tif'}", values, driver=driver, **options)
    with rasterio.open(source) as dataset:
        decoded = dataset.read(1)
    bounds = (499980.0 + 100, 100020.0 - 400, 499980.0 + 300, 100020.0 - 200)
    record = write_raster_subset(source, tmp_path / "subset.tif", bounds, product_id="P")
    with rasterio.open(tmp_path / "subset.tif") as subset:
        assert np.array_equal(subset.read(1), decoded[20:40, 10:30])
        assert subset.transform == Affine(10.0, 0.0, 500080.0, 0.0, -10.0, 99820.0)
        assert subset.dtypes[0] == "uint16" and subset.nodata is None
        assert subset.tags()["product_id"] == "P"
    assert record["source_window"] == {"col_off": 10, "row_off": 20, "width": 20, "height": 20}
    assert record["pixel_equality"] == "verified" and len(record["sha256"]) == 64


def _parent():
    digest = "a" * 64
    roles = {"b04": "B04_10m", "b08": "B08_10m", "scl": "SCL_20m", "calibration_xml": "product_metadata"}
    return {
        "schema_version": "geonaf.sentinel2-acquisition/v1", "product_id": "P", "collection": "sentinel-2-l2a",
        "acquired_at_utc": "2025-01-15T09:32:39.024000Z", "ingested_at_utc": "2026-10-16T17:10:20Z",
        "stac": {"source": "https://stac.example", "item_id": "P"}, "calibration": {"method": "BOA"},
        "assets": {role: {"id": asset_id, "product_id": "P", "path": f"{role}.jp2", "sha256": digest}
                   for role, asset_id in roles.items()},
        "integrity": {"mode": "strict", "source_assets": {role: {"status": "verified"} for role in roles}},
    }


def _manifest(**changes):
    raster = {"path": "x.tif", "sha256": "b" * 64, "pixel_equality": "verified"}
    assets = {"b04": dict(raster), "b08": dict(raster), "scl": dict(raster),
              "calibration_xml": {"path": "MTD.xml", "sha256": "a" * 64}}
    assets.update(changes.pop("assets", {}))
    parent = changes.pop("parent", _parent())
    return build_subset_manifest(parent_manifest=parent, parent_manifest_sha256="c" * 64, assets=assets,
                                 aoi={"id": "akanda"}, margin_m=200, software={},
                                 created_at=datetime(2026, 10, 26, tzinfo=timezone.utc))


def test_subset_manifest_keeps_parent_and_subset_digests():
    manifest = _manifest()
    assert manifest["parent"]["assets"]["b04"]["sha256"] == "a" * 64
    assert manifest["assets"]["b04"]["sha256"] == "b" * 64
    assert manifest["derivation"]["resampling"] == "none"


@pytest.mark.parametrize("change", [
    {"assets": {"b04": {"path": "../x.tif", "sha256": "b" * 64, "pixel_equality": "verified"}}},
    {"assets": {"scl": {"path": "x.tif", "sha256": "b" * 64}}},
    {"assets": {"calibration_xml": {"path": "MTD.xml", "sha256": "d" * 64}}},
])
def test_invalid_subset_assets_are_rejected(change):
    with pytest.raises(ValueError):
        _manifest(**change)


def test_unverified_parent_is_rejected():
    parent = _parent()
    parent["integrity"]["source_assets"]["b08"]["status"] = "unconfirmed"
    with pytest.raises(ValueError, match="intégrité stricte"):
        _manifest(parent=parent)


def test_resampled_subset_is_rejected():
    manifest = _manifest()
    manifest["derivation"]["resampling"] = "bilinear"
    with pytest.raises(ValueError, match="rééchantillonné"):
        validate_subset_manifest(manifest)
