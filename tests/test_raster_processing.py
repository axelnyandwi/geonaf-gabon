"""Tests sans réseau du découpage raster utilisé pour les bandes Sentinel-2."""

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin
from rasterio.warp import transform_geom
from shapely.geometry import Polygon, mapping

from src.data.raster_processing import clip_raster


def _write_raster(path, data, *, transform=None, crs="EPSG:32632", nodata=0):
    """Crée un petit raster synthétique pour isoler le découpage."""
    if transform is None:
        transform = from_origin(100, 200, 10, 10)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=data.shape[0],
        width=data.shape[1],
        count=1,
        dtype=data.dtype,
        crs=crs,
        transform=transform,
        nodata=nodata,
    ) as destination:
        destination.write(data, 1)
    return path


def _geometry(bounds):
    """Retourne un polygone rectangulaire GeoJSON à partir de ses bornes."""
    min_x, min_y, max_x, max_y = bounds
    return mapping(
        Polygon(
            [
                (min_x, min_y),
                (max_x, min_y),
                (max_x, max_y),
                (min_x, max_y),
                (min_x, min_y),
            ]
        )
    )


def test_clip_preserves_pixel_values_resolution_and_crop_transform(tmp_path):
    """Vérifie pixels, résolution et translation affine après un crop aligné."""
    source_data = np.arange(1, 17, dtype=np.uint16).reshape(4, 4)
    source = _write_raster(tmp_path / "source.tif", source_data)
    output = tmp_path / "nested" / "clip.tif"

    result = clip_raster(
        source,
        _geometry((110, 180, 130, 200)),
        output,
        geometry_crs="EPSG:32632",
    )

    assert result == output
    with rasterio.open(result) as clipped:
        np.testing.assert_array_equal(
            clipped.read(1),
            np.array([[2, 3], [6, 7]], dtype=np.uint16),
        )
        assert clipped.crs.to_epsg() == 32632
        assert clipped.res == (10, 10)
        assert clipped.transform == from_origin(110, 200, 10, 10)
        assert clipped.bounds.left == 110
        assert clipped.bounds.top == 200


def test_clip_aoi_partly_outside_raster_keeps_only_intersection(tmp_path):
    """Une AOI débordante est limitée à l’intersection disponible dans le raster."""
    source_data = np.arange(1, 17, dtype=np.uint16).reshape(4, 4)
    source = _write_raster(tmp_path / "source.tif", source_data)
    output = tmp_path / "partial.tif"

    clip_raster(
        source,
        _geometry((130, 170, 150, 190)),
        output,
        geometry_crs="EPSG:32632",
    )

    with rasterio.open(output) as clipped:
        np.testing.assert_array_equal(
            clipped.read(1),
            np.array([[8], [12]], dtype=np.uint16),
        )
        assert clipped.bounds.left == 130
        assert clipped.bounds.right == 140
        assert clipped.bounds.bottom == 170
        assert clipped.bounds.top == 190


def test_clip_aoi_without_raster_intersection_raises_value_error(tmp_path):
    """Une AOI sans recouvrement doit produire une erreur, pas un raster vide."""
    source = _write_raster(
        tmp_path / "source.tif", np.ones((4, 4), dtype=np.uint16)
    )

    with pytest.raises(ValueError, match="do not overlap"):
        clip_raster(
            source,
            _geometry((150, 170, 170, 190)),
            tmp_path / "outside.tif",
            geometry_crs="EPSG:32632",
        )


def test_clip_reprojects_geometry_from_a_different_crs(tmp_path):
    """La géométrie UTM est transformée vers le CRS géographique du raster."""
    source_data = np.arange(1, 17, dtype=np.uint16).reshape(4, 4)
    source = _write_raster(
        tmp_path / "geographic.tif",
        source_data,
        transform=from_origin(9.0, 1.0, 0.01, 0.01),
        crs="EPSG:4326",
    )
    polygon_wgs84 = _geometry((9.012, 0.982, 9.028, 0.998))
    polygon_utm = transform_geom(
        "EPSG:4326",
        "EPSG:32632",
        polygon_wgs84,
    )
    output = tmp_path / "reprojected.tif"

    clip_raster(
        source,
        polygon_utm,
        output,
        geometry_crs="EPSG:32632",
    )

    with rasterio.open(output) as clipped:
        assert clipped.crs.to_epsg() == 4326
        assert clipped.shape == (2, 2)
        np.testing.assert_array_equal(
            clipped.read(1),
            np.array([[2, 3], [6, 7]], dtype=np.uint16),
        )
        assert clipped.res == pytest.approx((0.01, 0.01))


def test_clip_preserves_source_nodata_and_fills_masked_pixels(tmp_path):
    """Le NoData source reste marqué et masque les cellules hors de l’AOI."""
    source_data = np.arange(1, 17, dtype=np.int16).reshape(4, 4)
    source_data[1, 1] = -9999
    source = _write_raster(
        tmp_path / "with-nodata.tif", source_data, nodata=-9999
    )
    triangle = mapping(Polygon([(100, 200), (140, 200), (100, 160)]))
    output = tmp_path / "masked.tif"

    clip_raster(source, triangle, output, geometry_crs="EPSG:32632")

    with rasterio.open(output) as clipped:
        result = clipped.read(1)
        masked = clipped.read(1, masked=True)
        assert clipped.nodata == -9999
        assert result[0, 0] == 1
        assert result[1, 1] == -9999  # NoData du raster source
        assert result[3, 3] == -9999  # Hors de l’AOI, rempli en NoData
        assert np.ma.getmaskarray(masked)[1, 1]
        assert np.ma.getmaskarray(masked)[3, 3]


def test_clip_uses_zero_as_nodata_when_source_has_none(tmp_path):
    """Sans NoData déclaré, les pixels hors masque reçoivent la valeur de repli 0."""
    source = _write_raster(
        tmp_path / "no-nodata.tif",
        np.ones((4, 4), dtype=np.uint16),
        nodata=None,
    )
    triangle = mapping(Polygon([(100, 200), (140, 200), (100, 160)]))
    output = tmp_path / "fallback-nodata.tif"

    clip_raster(source, triangle, output, geometry_crs="EPSG:32632")

    with rasterio.open(output) as clipped:
        result = clipped.read(1)
        assert clipped.nodata == 0
        assert result[0, 0] == 1
        assert result[3, 3] == 0


def test_clip_missing_raster_raises_file_not_found(tmp_path):
    """Une source absente est signalée avant l’appel aux outils Rasterio."""
    with pytest.raises(FileNotFoundError):
        clip_raster(
            tmp_path / "missing.tif",
            _geometry((110, 180, 130, 200)),
            tmp_path / "output.tif",
            geometry_crs="EPSG:32632",
        )


def test_clip_raster_without_crs_raises_value_error(tmp_path):
    """Sans CRS source, le reprojetage de l’AOI ne peut pas être défini."""
    source = _write_raster(
        tmp_path / "no-crs.tif",
        np.ones((4, 4), dtype=np.uint16),
        crs=None,
    )

    with pytest.raises(ValueError, match="CRS du raster manquant"):
        clip_raster(
            source,
            _geometry((110, 180, 130, 200)),
            tmp_path / "output.tif",
            geometry_crs="EPSG:32632",
        )
