"""Tests synthétiques de l'alignement NDVI multitemporel."""

import numpy as np
import pytest
import rasterio
from affine import Affine
from rasterio.transform import from_bounds, from_origin

from src.geo import temporal


def _write_raster(
    path, array, *, crs="EPSG:3857", transform, nodata=None, valid_mask=None
):
    array = np.asarray(array)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=array.shape[1],
        height=array.shape[0],
        count=1,
        dtype=array.dtype,
        crs=crs,
        transform=transform,
        nodata=nodata,
    ) as dst:
        dst.write(array, 1)
        if valid_mask is not None:
            dst.write_mask(np.asarray(valid_mask, dtype=np.uint8) * 255)
    return path


def _make_pair(tmp_path, first, second, *, first_transform=None,
               second_transform=None, first_scl=None, second_scl=None,
               first_scl_mask=None, second_scl_mask=None,
               first_nodata=None, second_nodata=None, crs1="EPSG:3857",
               crs2=None):
    first_transform = first_transform or from_origin(0, 4, 1, 1)
    second_transform = second_transform or first_transform
    crs2 = crs2 or crs1
    first_scl = np.full(np.asarray(first).shape, 4, dtype=np.uint8) if first_scl is None else first_scl
    second_scl = np.full(np.asarray(second).shape, 4, dtype=np.uint8) if second_scl is None else second_scl

    paths = {
        "ndvi1": tmp_path / "ndvi-date-1.tif",
        "ndvi2": tmp_path / "ndvi-date-2.tif",
        "scl1": tmp_path / "scl-date-1.tif",
        "scl2": tmp_path / "scl-date-2.tif",
    }
    _write_raster(paths["ndvi1"], first, transform=first_transform,
                  crs=crs1, nodata=first_nodata)
    _write_raster(paths["ndvi2"], second, transform=second_transform,
                  crs=crs2, nodata=second_nodata)
    _write_raster(paths["scl1"], first_scl, transform=first_transform,
                  crs=crs1, nodata=0, valid_mask=first_scl_mask)
    _write_raster(paths["scl2"], second_scl, transform=second_transform,
                  crs=crs2, nodata=0, valid_mask=second_scl_mask)
    return paths


def _compare(paths, **kwargs):
    return temporal.harmonize_ndvi_pair(
        paths["ndvi1"], paths["ndvi2"], paths["scl1"], paths["scl2"],
        **kwargs,
    )


def test_identical_grids_are_preserved_without_reprojection(tmp_path, monkeypatch):
    """Une grille identique garde ses valeurs exactes et évite un warp inutile."""
    values_1 = np.array([[0.1, 0.2], [0.3, 0.4]], dtype=np.float32)
    values_2 = values_1 + 0.25
    paths = _make_pair(tmp_path, values_1, values_2)

    def unexpected_reprojection(*args, **kwargs):
        raise AssertionError("Aucune reprojection n'est attendue pour deux grilles identiques.")

    monkeypatch.setattr(temporal, "reproject", unexpected_reprojection)
    result = _compare(paths)

    assert result.grid.crs == rasterio.crs.CRS.from_epsg(3857)
    assert result.grid.resolution == (1.0, 1.0)
    assert result.grid.transform == from_origin(0, 4, 1, 1)
    assert result.grid.width == result.grid.height == 2
    assert result.grid.bounds == (0.0, 2.0, 2.0, 4.0)
    np.testing.assert_array_equal(result.ndvi_date_1, values_1)
    np.testing.assert_allclose(result.delta_ndvi, 0.25, atol=1e-7)
    assert result.comparison_mask.all()


def test_common_scl_policy_excludes_classes_outside_land_surface(tmp_path):
    """Les deux dates appliquent la même politique : 2, 4, 5 inclus ; 7 et eau exclus."""
    classes = np.array([[2, 4, 5, 7, 6, 3]], dtype=np.uint8)
    transform = from_origin(0, 1, 1, 1)
    values = np.full((1, 6), 0.4, dtype=np.float32)
    paths = _make_pair(
        tmp_path, values, values,
        first_transform=transform,
        first_scl=classes,
    )
    # The second date defaults to class 4 everywhere.
    result = _compare(paths, scl_policy="land_surface")

    np.testing.assert_array_equal(
        result.valid_date_1,
        np.array([[True, True, True, False, False, False]]),
    )
    assert result.comparison_mask.tolist() == [[True, True, True, False, False, False]]
    assert np.isnan(result.delta_ndvi[0, 3:]).all()


def test_default_policy_excludes_shadows_in_either_date(tmp_path):
    values = np.full((1, 4), 0.4, dtype=np.float32)
    paths = _make_pair(tmp_path, values, values,
                       first_scl=np.array([[2, 4, 5, 4]], dtype=np.uint8),
                       second_scl=np.array([[4, 4, 5, 2]], dtype=np.uint8))
    result = _compare(paths)
    assert result.comparison_mask.tolist() == [[False, True, True, False]]
    assert np.isnan(result.delta_ndvi[0, [0, 3]]).all()


def test_nodata_in_either_date_is_never_compared(tmp_path):
    """Un NoData source reste invalide même si l'autre date a une valeur valide."""
    nodata = -9999.0
    first = np.array([[0.2, nodata], [0.4, 0.5]], dtype=np.float32)
    second = np.full((2, 2), 0.7, dtype=np.float32)
    paths = _make_pair(
        tmp_path, first, second,
        first_nodata=nodata,
    )

    result = _compare(paths)

    assert not result.valid_date_1[0, 1]
    assert result.valid_date_2[0, 1]
    assert not result.comparison_mask[0, 1]
    assert np.isnan(result.delta_ndvi[0, 1])


def test_internal_scl_mask_excludes_even_nonzero_class_values(tmp_path):
    """Le masque SCL prime sur sa valeur stockée, même si elle vaut 4."""
    values = np.full((2, 2), 0.4, dtype=np.float32)
    scl = np.full((2, 2), 4, dtype=np.uint8)
    scl_valid = np.array([[True, False], [True, True]])
    paths = _make_pair(
        tmp_path, values, values,
        first_scl=scl,
        first_scl_mask=scl_valid,
    )

    result = _compare(paths)

    assert not result.valid_date_1[0, 1]
    assert result.valid_date_2[0, 1]
    assert not result.comparison_mask[0, 1]
    assert np.isnan(result.delta_ndvi[0, 1])


def test_partial_extent_uses_only_inward_snapped_intersection(tmp_path):
    """Les dates décalées produisent une grille limitée à leur emprise commune."""
    first = np.full((4, 4), 0.2, dtype=np.float32)
    second = np.full((4, 2), 0.6, dtype=np.float32)
    paths = _make_pair(
        tmp_path, first, second,
        second_transform=from_origin(2, 4, 1, 1),
    )

    result = _compare(paths)

    assert result.grid.bounds == (2.0, 0.0, 4.0, 4.0)
    assert (result.grid.height, result.grid.width) == (4, 2)
    assert result.comparison_mask.all()
    np.testing.assert_allclose(result.delta_ndvi, 0.4)


def test_partial_pixel_overlap_snaps_grid_inside_both_footprints(tmp_path):
    """L'intersection est arrondie vers l'intérieur pour exclure les demi-pixels."""
    first = np.full((4, 4), 0.2, dtype=np.float32)
    second = np.full((4, 3), 0.6, dtype=np.float32)
    paths = _make_pair(
        tmp_path, first, second,
        second_transform=from_origin(1.5, 4, 1, 1),
    )

    grid = temporal.build_common_grid(paths["ndvi1"], paths["ndvi2"])

    assert grid.bounds == (2.0, 0.0, 4.0, 4.0)


def test_non_intersecting_rasters_raise_clear_error(tmp_path):
    """Une absence de recouvrement ne doit pas produire une grille vide."""
    paths = _make_pair(
        tmp_path,
        np.ones((2, 2), dtype=np.float32),
        np.ones((2, 2), dtype=np.float32),
        first_transform=from_origin(0, 2, 1, 1),
        second_transform=from_origin(10, 12, 1, 1),
    )

    with pytest.raises(ValueError, match="aucune intersection"):
        temporal.build_common_grid(paths["ndvi1"], paths["ndvi2"])


def test_different_resolution_aligns_to_first_dates_grid(tmp_path):
    """La résolution date 1 définit la grille quand date 2 est plus grossière."""
    first = np.full((4, 4), 0.2, dtype=np.float32)
    second = np.full((2, 2), 0.6, dtype=np.float32)
    paths = _make_pair(
        tmp_path, first, second,
        second_transform=from_origin(0, 4, 2, 2),
    )

    result = _compare(paths)

    assert result.grid.resolution == (1.0, 1.0)
    assert (result.grid.height, result.grid.width) == (4, 4)
    assert result.comparison_mask.any()
    assert np.isnan(result.delta_ndvi[~result.comparison_mask]).all()


def test_scl_alignment_reads_only_an_exact_target_window(tmp_path, monkeypatch):
    """Une sous-grille alignée lit sa fenêtre, sans charger le raster entier."""
    source = np.arange(100, dtype=np.uint8).reshape(10, 10) % 12
    path = _write_raster(
        tmp_path / "scl-large.tif",
        source,
        transform=from_origin(0, 10, 1, 1),
        nodata=0,
    )
    target = temporal.RasterGrid(
        crs=rasterio.crs.CRS.from_epsg(3857),
        resolution=(1.0, 1.0),
        transform=from_origin(3, 7, 1, 1),
        width=2,
        height=2,
    )
    reads = []
    original_open = rasterio.open

    class ReadSpy:
        def __init__(self, dataset):
            self.dataset = dataset

        def __enter__(self):
            self.dataset.__enter__()
            return self

        def __exit__(self, *args):
            return self.dataset.__exit__(*args)

        def read(self, *args, **kwargs):
            reads.append(kwargs.get("window"))
            return self.dataset.read(*args, **kwargs)

        def __getattr__(self, name):
            return getattr(self.dataset, name)

    monkeypatch.setattr(
        temporal.rasterio,
        "open",
        lambda *args, **kwargs: ReadSpy(original_open(*args, **kwargs)),
    )
    actual = temporal.scl_policy_mask_on_grid(path, target, "land_surface")

    assert reads and reads[0] is not None
    assert reads[0].width == 2 and reads[0].height == 2
    np.testing.assert_array_equal(actual, np.isin(source[3:5, 3:5], [2, 4, 5]))


def test_different_crs_is_warped_to_reference_crs(tmp_path):
    """Une source en degrés est comparée sur la grille métrique de date 1."""
    from rasterio.warp import transform_bounds

    metric_bounds = (0, 0, 2000, 2000)
    geographic_bounds = transform_bounds(
        "EPSG:3857", "EPSG:4326", *metric_bounds, densify_pts=21
    )
    geographic_transform = from_bounds(*geographic_bounds, 2, 2)
    paths = _make_pair(
        tmp_path,
        np.full((2, 2), 0.2, dtype=np.float32),
        np.full((2, 2), 0.6, dtype=np.float32),
        first_transform=from_origin(0, 2000, 1000, 1000),
        second_transform=geographic_transform,
        crs1="EPSG:3857",
        crs2="EPSG:4326",
    )

    result = _compare(paths)

    assert result.grid.crs == rasterio.crs.CRS.from_epsg(3857)
    assert (result.grid.height, result.grid.width) == (2, 2)
    assert result.comparison_mask.any()


def test_mask_weighted_bilinear_does_not_fill_nodata_neighbourhood(tmp_path):
    """Une interpolation touchant un pixel NoData reste invalide par prudence."""
    nodata = -9999.0
    first = np.array([[0.2, nodata, 0.8]], dtype=np.float32)
    second = np.full((1, 3), 0.5, dtype=np.float32)
    source_transform = from_origin(0, 1, 1, 1)
    paths = _make_pair(
        tmp_path, first, second,
        first_transform=source_transform,
        first_nodata=nodata,
    )
    shifted_grid = temporal.RasterGrid(
        crs=rasterio.crs.CRS.from_epsg(3857),
        resolution=(1.0, 1.0),
        transform=Affine(1, 0, 0.5, 0, -1, 1),
        width=2,
        height=1,
    )

    result = _compare(paths, grid=shifted_grid)

    assert not result.valid_date_1.any()
    assert not result.comparison_mask.any()
    assert np.isnan(result.delta_ndvi).all()


def test_date_order_sets_delta_sign(tmp_path):
    """L'ordre des dates est explicite : delta = date 2 moins date 1."""
    paths = _make_pair(
        tmp_path,
        np.full((2, 2), 0.7, dtype=np.float32),
        np.full((2, 2), 0.2, dtype=np.float32),
    )

    result = _compare(paths)

    np.testing.assert_allclose(result.delta_ndvi, -0.5)


def test_invalid_scl_policy_is_rejected(tmp_path):
    """Une politique inconnue ne doit pas élargir implicitement les classes admises."""
    paths = _make_pair(
        tmp_path,
        np.full((2, 2), 0.2, dtype=np.float32),
        np.full((2, 2), 0.4, dtype=np.float32),
    )

    with pytest.raises(ValueError, match="Politique SCL inconnue"):
        _compare(paths, scl_policy="all_pixels")


def test_custom_grid_must_match_its_affine_resolution(tmp_path):
    """Une grille explicite incohérente est refusée avant tout calcul."""
    paths = _make_pair(
        tmp_path,
        np.full((2, 2), 0.2, dtype=np.float32),
        np.full((2, 2), 0.4, dtype=np.float32),
    )
    invalid_grid = temporal.RasterGrid(
        crs=rasterio.crs.CRS.from_epsg(3857),
        resolution=(2.0, 2.0),
        transform=from_origin(0, 4, 1, 1),
        width=2,
        height=2,
    )

    with pytest.raises(ValueError, match="ne correspond pas"):
        _compare(paths, grid=invalid_grid)
