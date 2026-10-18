"""Tests for calibrated NDVI and the historical file-writing adapter."""

import json
from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from src.data.preparation import calculate_ndvi as write_ndvi
from src.geo.vegetation_indices import calculate_ndvi


def _write_band(path, values, *, crs="EPSG:32632", transform=None, nodata=None):
    values = np.asarray(values)
    if transform is None:
        transform = from_origin(0, values.shape[0], 1, 1)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=values.shape[0],
        width=values.shape[1],
        count=1,
        dtype=values.dtype,
        crs=crs,
        transform=transform,
        nodata=nodata,
    ) as destination:
        destination.write(values, 1)
    return path


def test_canonical_ndvi_applies_offsets_and_masks_invalid_denominators(tmp_path):
    """BOA offsets are applied before the ratio; zero reflectance sums stay NaN."""
    red_path = _write_band(
        tmp_path / "red.tif",
        [[0, 1000, 2000]],
        nodata=0,
    )
    nir_path = _write_band(
        tmp_path / "nir.tif",
        [[1000, 1000, 5000]],
        nodata=0,
    )

    ndvi, _ = calculate_ndvi(
        red_path,
        nir_path,
        None,
        red_offset=-1000,
        nir_offset=-1000,
        quantification_value=10000,
    )

    assert np.isnan(ndvi[0, 0])  # Source NoData / zero DN
    assert np.isnan(ndvi[0, 1])  # Corrected denominator is zero
    assert ndvi[0, 2] == pytest.approx(0.6)


@pytest.mark.parametrize(
    "nir_options",
    [
        {"crs": "EPSG:32633"},
        {"transform": from_origin(1, 1, 1, 1)},
        {"values": [[1000, 1000]]},
    ],
)
def test_canonical_ndvi_rejects_unaligned_grids(tmp_path, nir_options):
    """CRS, affine transform, and shape must all match before pixel arithmetic."""
    red_path = _write_band(tmp_path / "red.tif", [[1000]], nodata=0)
    values = nir_options.pop("values", [[1000]])
    nir_path = _write_band(tmp_path / "nir.tif", values, nodata=0, **nir_options)

    with pytest.raises(ValueError, match="Bandes non alignées"):
        calculate_ndvi(
            red_path,
            nir_path,
            None,
            red_offset=0,
            nir_offset=0,
            quantification_value=1,
        )


def test_canonical_ndvi_rejects_missing_crs_and_invalid_calibration(tmp_path):
    """A missing coordinate system or non-finite coefficient cannot pass silently."""
    red_path = _write_band(tmp_path / "red.tif", [[1000]], crs=None)
    nir_path = _write_band(tmp_path / "nir.tif", [[2000]], crs=None)

    with pytest.raises(ValueError, match="CRS manquant"):
        calculate_ndvi(
            red_path,
            nir_path,
            None,
            red_offset=0,
            nir_offset=0,
            quantification_value=1,
        )

    valid_red = _write_band(tmp_path / "valid-red.tif", [[1000]])
    valid_nir = _write_band(tmp_path / "valid-nir.tif", [[2000]])
    with pytest.raises(ValueError, match="Paramètres de calibration invalides"):
        calculate_ndvi(
            valid_red,
            valid_nir,
            None,
            red_offset=float("nan"),
            nir_offset=0,
            quantification_value=1,
        )


def test_historical_adapter_preserves_no_offset_ratio_for_calibrated_reflectance(tmp_path):
    """The compatibility call does not apply Sentinel offsets a second time."""
    red_path = _write_band(tmp_path / "red.tif", np.array([[0.1]], dtype=np.float32))
    nir_path = _write_band(tmp_path / "nir.tif", np.array([[0.4]], dtype=np.float32))
    output_path = tmp_path / "ndvi.tif"

    result = write_ndvi(red_path, nir_path, output_path, inputs_are_reflectance=True)

    assert result == output_path
    with rasterio.open(result) as output:
        assert output.read(1)[0, 0] == pytest.approx(0.6)
        assert output.dtypes[0] == "float32"


def test_historical_adapter_loads_offsets_from_product_xml(tmp_path):
    """The old output-path API can opt into product-specific DN calibration."""
    xml_path = tmp_path / "MTD_MSIL2A.xml"
    xml_path.write_text(
        """<Product>
        <BOA_QUANTIFICATION_VALUE>10000</BOA_QUANTIFICATION_VALUE>
        <BOA_ADD_OFFSET band_id="3">-1000</BOA_ADD_OFFSET>
        <BOA_ADD_OFFSET band_id="7">-1000</BOA_ADD_OFFSET>
        </Product>""",
        encoding="utf-8",
    )
    red_path = _write_band(tmp_path / "red.tif", [[2000]])
    nir_path = _write_band(tmp_path / "nir.tif", [[5000]])
    output_path = tmp_path / "calibrated-ndvi.tif"

    write_ndvi(red_path, nir_path, output_path, calibration_xml=xml_path)

    with rasterio.open(output_path) as output:
        assert output.read(1)[0, 0] == pytest.approx(0.6)


def test_adapter_rejects_ambiguous_inputs_without_writing(tmp_path):
    red = _write_band(tmp_path / "red.tif", [[2000]])
    nir = _write_band(tmp_path / "nir.tif", [[5000]])
    output = tmp_path / "ndvi.tif"
    with pytest.raises(ValueError, match="calibration_xml"):
        write_ndvi(red, nir, output)
    assert not output.exists()


def test_adapter_rejects_conflicting_input_units(tmp_path):
    with pytest.raises(ValueError, match="incompatibles"):
        write_ndvi(tmp_path / "red", tmp_path / "nir", tmp_path / "out",
                   calibration_xml=tmp_path / "xml", inputs_are_reflectance=True)


def test_notebook_calibrates_dn_and_excludes_non_land_scl(tmp_path):
    """Run the notebook's calculation cells offline, with every SCL class."""
    xml = tmp_path / "MTD_MSIL2A.xml"
    xml.write_text('''<Product>
    <BOA_QUANTIFICATION_VALUE>10000</BOA_QUANTIFICATION_VALUE>
    <BOA_ADD_OFFSET band_id="3">-1000</BOA_ADD_OFFSET>
    <BOA_ADD_OFFSET band_id="7">-1000</BOA_ADD_OFFSET>
    </Product>''', encoding="utf-8")
    red = _write_band(tmp_path / "red.tif", np.full((1, 12), 2000, dtype=np.uint16))
    nir = _write_band(tmp_path / "nir.tif", np.full((1, 12), 5000, dtype=np.uint16))
    scl = _write_band(tmp_path / "scl.tif", np.arange(12, dtype=np.uint8)[None, :])
    notebook = json.loads((Path(__file__).resolve().parents[1] /
                           "notebooks/01_sentinel2_discovery.ipynb").read_text(encoding="utf-8"))
    namespace = {"np": np, "b04_local_path": red, "b08_local_path": nir,
                 "download_band": lambda role: scl}
    for index in (47, 48, 58, 59, 63, 64, 65, 66):
        exec("".join(notebook["cells"][index]["source"]), namespace)
    result = namespace["ndvi_clean"]
    assert np.flatnonzero(np.isfinite(result)).tolist() == [4, 5]
    np.testing.assert_allclose(result[0, [4, 5]], 0.6)
    xml.unlink()
    with pytest.raises(FileNotFoundError):
        exec("".join(notebook["cells"][47]["source"]), namespace)
