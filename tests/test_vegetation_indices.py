
import numpy as np
import rasterio
from rasterio.transform import from_origin

from src.geo.vegetation_indices import calculate_ndvi


def test_ndvi_radiometric_calibration(tmp_path):
    """Vérifie le calcul NDVI avec les offsets Sentinel-2."""

    red_path = tmp_path / "B04.tif"
    nir_path = tmp_path / "B08.tif"

    profile = {
        "driver": "GTiff",
        "height": 1,
        "width": 1,
        "count": 1,
        "dtype": "uint16",
        "crs": "EPSG:32632",
        "transform": from_origin(0, 10, 10, 10),
        "nodata": 0,
    }

    # Valeurs DN fictives :
    # B04 = 2000, B08 = 5000
    for path, value in [
        (red_path, 2000),
        (nir_path, 5000),
    ]:
        with rasterio.open(path, "w", **profile) as dst:
            dst.write(
                np.array([[value]], dtype=np.uint16),
                1,
            )

    valid_mask = np.array([[True]])

    ndvi, _ = calculate_ndvi(
        red_path=red_path,
        nir_path=nir_path,
        valid_mask=valid_mask,
        red_offset=-1000,
        nir_offset=-1000,
        quantification_value=10000,
    )

    # Réflectances corrigées :
    # Rouge = 0.1, infrarouge = 0.4
    # NDVI = (0.4 - 0.1) / (0.4 + 0.1) = 0.6
    assert np.isclose(ndvi[0, 0], 0.6)


def test_ndvi_wrong_offset_sign(tmp_path):
    """Vérifie que le signe de l'offset influence le NDVI."""

    red_path = tmp_path / "B04.tif"
    nir_path = tmp_path / "B08.tif"

    profile = {
        "driver": "GTiff",
        "height": 1,
        "width": 1,
        "count": 1,
        "dtype": "uint16",
        "crs": "EPSG:32632",
        "transform": from_origin(0, 10, 10, 10),
        "nodata": 0,
    }

    for path, value in [(red_path, 2000), (nir_path, 5000)]:
        with rasterio.open(path, "w", **profile) as dst:
            dst.write(np.array([[value]], dtype=np.uint16), 1)

    mask = np.array([[True]])

    correct, _ = calculate_ndvi(
        red_path, nir_path, mask,
        red_offset=-1000,
        nir_offset=-1000,
        quantification_value=10000,
    )

    incorrect, _ = calculate_ndvi(
        red_path, nir_path, mask,
        red_offset=1000,
        nir_offset=1000,
        quantification_value=10000,
    )

    assert np.isclose(correct[0, 0], 0.6)
    assert np.isclose(incorrect[0, 0], 1 / 3)
    assert not np.isclose(correct[0, 0], incorrect[0, 0])
