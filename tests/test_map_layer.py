"""Display reprojection must preserve orientation, validity and science inputs."""

import base64
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
from shapely.geometry import box

from src.geo.temporal import RasterGrid
from src.services.map_layer import prepare_ndvi_layer


def _result(tmp_path, crs="EPSG:3857", transform=None, geometry=None):
    aoi = tmp_path / "aoi.geojson"
    transform = transform or from_origin(0, 200, 100, 100)
    geometry = geometry if geometry is not None else box(1, 1, 199, 199)
    gpd.GeoDataFrame({"private_path": ["not-for-browser"], "geometry": [geometry]}, crs=crs).to_file(aoi)
    values = np.array([[-0.8, 0.8, 0.1], [0.2, np.nan, 0.9]], dtype=np.float32)
    return SimpleNamespace(
        common_grid=RasterGrid(CRS.from_string(crs), (abs(transform.a), abs(transform.e)), transform, 3, 2),
        comparison=SimpleNamespace(ndvi_date_2=values, valid_date_2=np.isfinite(values)),
        later=SimpleNamespace(acquired_at_utc=datetime(2025, 1, 25, tzinfo=timezone.utc)),
        scl_policy="usable_land", aoi_path=str(aoi),
    )


def test_orientation_valid_mask_and_outside_are_distinct(tmp_path):
    result = _result(tmp_path)
    before = result.comparison.ndvi_date_2.copy()
    layer = prepare_ndvi_layer(result)
    png = np.asarray(Image.open(BytesIO(base64.b64decode(layer["image"].split(",", 1)[1]))))
    assert png.shape == (2, 3, 4)
    assert png[0, 0, 0] > png[0, 0, 1]  # North-west: low NDVI, red.
    assert png[0, 1, 1] > png[0, 1, 0]  # North-east: high NDVI, green.
    assert png[1, 1].tolist() == [209, 213, 219, 255]  # Masked inside AOI.
    assert np.all(png[:, 2, 3] == 0)  # Exterior is transparent, never grey.
    tl, tr, br, bl = layer["coordinates"]
    assert tl[0] < tr[0] and tl[1] > bl[1]
    assert tr[1] == pytest.approx(tl[1]) and br[1] == pytest.approx(bl[1])
    np.testing.assert_array_equal(before, result.comparison.ndvi_date_2)


def test_public_payload_has_no_paths_or_aoi_properties(tmp_path):
    layer = prepare_ndvi_layer(_result(tmp_path))
    public = json.dumps(layer)
    assert str(tmp_path) not in public and "not-for-browser" not in public
    assert "manifest" not in public and "aoi_path" not in public
    assert layer["aoi"]["properties"] == {}
    assert layer["display_crs"] == "EPSG:3857"
    assert layer["scl_policy"] == "usable_land"


def test_utm_reprojects_to_expected_akanda_coordinates_and_limits_size(tmp_path):
    result = _result(tmp_path, crs="EPSG:32632", transform=from_origin(537000, 63000, 100, 100),
                     geometry=box(537001, 62801, 537199, 62999))
    layer = prepare_ndvi_layer(result, max_size=2)
    assert max(layer["width"], layer["height"]) <= 2
    for lon, lat in layer["coordinates"]:
        assert 9 < lon < 10 and 0 < lat < 1


def test_disallows_non_quantitative_policy(tmp_path):
    result = _result(tmp_path)
    result.scl_policy = "land_surface"
    with pytest.raises(ValueError, match="usable_land"):
        prepare_ndvi_layer(result)
