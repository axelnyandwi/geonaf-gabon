"""Tests synthétiques des politiques Sentinel-2 SCL."""

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from src.data.cloud_mask import create_scl_mask, scl_mask_from_values
from src.data.preparation import mask_ndvi_with_scl


def _write_raster(path, values, *, transform, nodata=None, valid_mask=None):
    values = np.asarray(values)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=values.shape[0],
        width=values.shape[1],
        count=1,
        dtype=values.dtype,
        crs="EPSG:32632",
        transform=transform,
        nodata=nodata,
    ) as destination:
        destination.write(values, 1)
        if valid_mask is not None:
            destination.write_mask(np.asarray(valid_mask, dtype=np.uint8) * 255)
    return path


def test_policies_separate_observation_land_vegetation_and_bare_soil():
    """Class 7 is observed but unclassified; class 2 is a land-surface candidate."""
    values = np.arange(12, dtype=np.uint8).reshape(1, 12)
    source_valid = np.ones(values.shape, dtype=bool)
    source_valid[0, 4] = False

    observed = scl_mask_from_values(values, policy="observation", valid_mask=source_valid)
    land_surface = scl_mask_from_values(values, policy="land_surface", valid_mask=source_valid)
    usable_land = scl_mask_from_values(values, policy="usable_land", valid_mask=source_valid)
    vegetation = scl_mask_from_values(values, policy="vegetation", valid_mask=source_valid)
    bare_soil = scl_mask_from_values(values, policy="bare_soil", valid_mask=source_valid)

    assert observed.tolist() == [[False, True, True, True, False, True, True, True, True, True, True, True]]
    assert land_surface.tolist() == [[False, False, True, False, False, True, False, False, False, False, False, False]]
    assert usable_land.tolist() == [[False, False, False, False, False, True, False, False, False, False, False, False]]
    assert vegetation.tolist() == [[False, False, False, False, False, False, False, False, False, False, False, False]]
    assert bare_soil.tolist() == usable_land.tolist()


def test_scl_policy_rejects_unknown_names_and_wrong_validity_mask_shape():
    """Invalid policy arguments fail clearly instead of silently changing classes."""
    values = np.array([[2, 4]], dtype=np.uint8)

    with pytest.raises(ValueError, match="Politique SCL inconnue"):
        scl_mask_from_values(values, policy="all_land")
    with pytest.raises(ValueError, match="Dimensions du masque"):
        scl_mask_from_values(values, valid_mask=np.ones((2, 1), dtype=bool))


def test_create_scl_mask_keeps_legacy_policy_by_default_and_allows_broader_land(tmp_path):
    """The old default remains classes 4/5; callers can explicitly include class 2."""
    scl_path = _write_raster(
        tmp_path / "scl.tif",
        [[0, 2, 4, 5, 7]],
        transform=from_origin(0, 20, 10, 20),
        nodata=0,
        valid_mask=[[True, True, False, True, True]],
    )
    reference_path = _write_raster(
        tmp_path / "reference.tif",
        np.ones((1, 5), dtype=np.uint16),
        transform=from_origin(0, 20, 10, 20),
    )

    legacy_mask = create_scl_mask(scl_path, reference_path)
    land_mask = create_scl_mask(scl_path, reference_path, policy="land_surface")

    assert legacy_mask.tolist() == [[False, False, False, True, False]]
    assert land_mask.tolist() == [[False, True, False, True, False]]


def test_ndvi_scl_mask_resamples_and_honors_class_zero_and_dataset_mask(tmp_path):
    """Nearest-neighbour alignment keeps class 2 and excludes NoData and 7."""
    scl_path = _write_raster(
        tmp_path / "scl.tif",
        [[0, 2, 4, 7]],
        transform=from_origin(0, 40, 10, 40),
        nodata=0,
        valid_mask=[[True, True, False, True]],
    )
    ndvi_path = _write_raster(
        tmp_path / "ndvi.tif",
        np.full((1, 8), 0.4, dtype=np.float32),
        transform=from_origin(0, 40, 5, 40),
        nodata=np.nan,
    )
    output_path = tmp_path / "ndvi-masked.tif"

    mask_ndvi_with_scl(ndvi_path, scl_path, output_path)

    with rasterio.open(output_path) as result:
        values = result.read(1)
        # Pixel-center nearest-neighbour sampling maps the first SCL cell
        # (class 0) to NDVI columns 0-1 and class 2 to columns 2-3.
        assert np.isnan(values[0, :2]).all()
        assert values[0, 2:4].tolist() == pytest.approx([0.4, 0.4])
        assert np.isnan(values[0, 4:]).all()
