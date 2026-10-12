
import numpy as np
import rasterio
from rasterio.transform import from_origin

from src.data.radiometric_calibration import read_sentinel2_calibration
from src.geo.vegetation_indices import calculate_ndvi


def test_xml_to_ndvi_integration(tmp_path):
    """Teste la calibration XML suivie du calcul NDVI."""

    # Métadonnées XML fictives mais représentatives
    xml_path = tmp_path / "MTD_MSIL2A.xml"

    xml_path.write_text(
        """
        <Level-2A_User_Product>
            <BOA_QUANTIFICATION_VALUE>
                10000
            </BOA_QUANTIFICATION_VALUE>
            <BOA_ADD_OFFSET band_id="3">-1000</BOA_ADD_OFFSET>
            <BOA_ADD_OFFSET band_id="7">-1000</BOA_ADD_OFFSET>
        </Level-2A_User_Product>
        """,
        encoding="utf-8",
    )

    calibration = read_sentinel2_calibration(xml_path)

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

    red_path = tmp_path / "B04.tif"
    nir_path = tmp_path / "B08.tif"

    for path, value in [(red_path, 2000), (nir_path, 5000)]:
        with rasterio.open(path, "w", **profile) as dst:
            dst.write(
                np.array([[value]], dtype=np.uint16),
                1,
            )

    ndvi, _ = calculate_ndvi(
        red_path=red_path,
        nir_path=nir_path,
        valid_mask=np.array([[True]]),
        red_offset=calibration["offsets"]["B04"],
        nir_offset=calibration["offsets"]["B08"],
        quantification_value=calibration["quantification_value"],
    )

    assert np.isclose(ndvi[0, 0], 0.6)
