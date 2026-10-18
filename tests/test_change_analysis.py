"""Tests synthétiques des indicateurs de changement NDVI."""

import geopandas as gpd
import numpy as np
import pytest
import rasterio
from affine import Affine
from pyproj import Transformer
from shapely import (
    area as geometry_area,
    intersection as geometry_intersection,
    polygons,
    transform as transform_geometry,
)
from shapely.geometry import MultiPolygon, Polygon, box
from rasterio.transform import from_origin

from src.geo.temporal import (
    RasterGrid,
    TemporalComparison,
    harmonize_ndvi_pair,
)
from src.geo.change_analysis import (
    ChangeClass,
    analyze_ndvi_changes,
    load_equal_area_aoi,
    measure_mask_coverage,
    prepare_pixel_area_weights,
)


def _comparison(
    deltas,
    *,
    valid=None,
    date_1=None,
    transform=Affine(1, 0, 0, 0, -1, 2),
    crs="EPSG:6933",
):
    deltas = np.asarray(deltas, dtype=np.float32)
    shape = deltas.shape
    valid = np.ones(shape, dtype=bool) if valid is None else np.asarray(valid, dtype=bool)
    date_1 = (
        np.full(shape, 0.3, dtype=np.float32)
        if date_1 is None
        else np.asarray(date_1, dtype=np.float32)
    )
    date_2 = date_1 + deltas
    delta = date_2 - date_1
    delta[~valid] = np.nan
    grid = RasterGrid(
        crs=rasterio.crs.CRS.from_user_input(crs),
        resolution=(abs(transform.a), abs(transform.e)),
        transform=transform,
        width=shape[1],
        height=shape[0],
    )
    return TemporalComparison(
        grid=grid,
        ndvi_date_1=date_1,
        ndvi_date_2=date_2,
        valid_date_1=valid.copy(),
        valid_date_2=valid.copy(),
        comparison_mask=valid.copy(),
        delta_ndvi=delta,
    )


def test_reusable_area_weights_match_direct_cell_intersections(tmp_path, monkeypatch):
    """Le cache d'aires garde les intersections partielles exactes de l'AOI."""
    from shapely.geometry import box as shapely_box

    from src.geo import change_analysis

    aoi_path = tmp_path / "partial-aoi.gpkg"
    gpd.GeoDataFrame(
        {"geometry": [shapely_box(0.5, 0.5, 1.5, 1.5)]}, crs="EPSG:6933"
    ).to_file(aoi_path, driver="GPKG")
    grid = RasterGrid(
        crs=rasterio.crs.CRS.from_epsg(6933),
        resolution=(1.0, 1.0),
        transform=Affine(1, 0, 0, 0, -1, 2),
        width=2,
        height=2,
    )
    aoi = load_equal_area_aoi(aoi_path)
    mask = np.array([[True, False], [False, True]])

    direct = measure_mask_coverage(mask, grid, aoi)
    weights = prepare_pixel_area_weights(grid, aoi)
    prepared = measure_mask_coverage(mask, grid, aoi, pixel_area_weights=weights)

    assert weights.shape == (2, 2)
    assert weights.sum() == pytest.approx(aoi.area_m2, abs=1e-8)
    assert prepared.area_m2 == pytest.approx(direct.area_m2, rel=1e-12)
    assert prepared.coverage_percent == pytest.approx(direct.coverage_percent, abs=1e-10)
    assert prepared.pixel_count == direct.pixel_count

    def no_recomputation(*args, **kwargs):
        raise AssertionError("Les poids pré-calculés doivent être réutilisés.")

    monkeypatch.setattr(change_analysis, "_pixel_area_chunks", no_recomputation)
    reused = measure_mask_coverage(mask, grid, aoi, pixel_area_weights=weights)
    assert reused == prepared


def _write_aoi(path, geometries, crs="EPSG:6933"):
    frame = gpd.GeoDataFrame(
        {"geometry": geometries},
        crs=crs,
    )
    frame.to_file(path, driver="GPKG")
    return path


def _reference_pixel_area_weights(grid, aoi, *, full_affine=False):
    """Brute-force oracle for legacy or complete-affine cell geometry."""
    rows, cols = np.meshgrid(
        np.arange(grid.height, dtype=np.float64),
        np.arange(grid.width, dtype=np.float64),
        indexing="ij",
    )
    corners = []
    for col_offset, row_offset in (
        (0, 0),
        (1, 0),
        (1, 1),
        (0, 1),
        (0, 0),
    ):
        pixel_cols = cols + col_offset
        pixel_rows = rows + row_offset
        x = grid.transform.a * pixel_cols + grid.transform.c
        y = grid.transform.e * pixel_rows + grid.transform.f
        if full_affine:
            x += grid.transform.b * pixel_rows
            y += grid.transform.d * pixel_cols
        corners.append(np.stack((x, y), axis=-1))

    cells = polygons(np.stack(corners, axis=2).reshape(-1, 5, 2))
    transformer = Transformer.from_crs(grid.crs, "EPSG:6933", always_xy=True)
    cells_equal_area = transform_geometry(
        cells,
        transformer.transform,
        interleaved=False,
    )
    clipped = geometry_intersection(cells_equal_area, aoi.geometry)
    return geometry_area(clipped).reshape(grid.height, grid.width)


@pytest.mark.parametrize(
    ("geometry", "transform"),
    [
        (
            box(0.5, 0.25, 2.75, 2.5),
            Affine(1, 0, 0, 0, -1, 3),
        ),
        (
            Polygon(
                [(0.25, 0.25), (3.75, 0.25), (3.75, 2.75), (0.25, 2.75)],
                holes=[[(1.25, 1.0), (2.75, 1.0), (2.75, 2.0), (1.25, 2.0)]],
            ),
            Affine(1, 0, 0, 0, -1, 3),
        ),
        (
            MultiPolygon(
                [box(0.2, 0.2, 1.2, 1.2), box(2.8, 1.8, 3.8, 2.8)]
            ),
            Affine(1, 0, 0, 0, -1, 3),
        ),
    ],
    ids=["partial-boundaries", "interior-hole", "multipart"],
)
def test_area_weights_match_former_exact_algorithm_for_complex_aois(
    tmp_path, geometry, transform
):
    """Les poids, aires de masque et couvertures gardent les intersections exactes."""
    aoi_path = _write_aoi(tmp_path / "complex-aoi.gpkg", [geometry])
    aoi = load_equal_area_aoi(aoi_path)
    grid = RasterGrid(
        crs=rasterio.crs.CRS.from_epsg(6933),
        resolution=(1.0, 1.0),
        transform=transform,
        width=4,
        height=3,
    )

    reference = _reference_pixel_area_weights(grid, aoi)
    optimized = prepare_pixel_area_weights(grid, aoi)

    # 1e-8 m² is far below the reporting precision; rtol covers accumulated
    # float64 summation differences while retaining cell-level equivalence.
    np.testing.assert_allclose(optimized, reference, rtol=1e-12, atol=1e-8)
    assert optimized.sum(dtype=np.float64) == pytest.approx(
        reference.sum(dtype=np.float64), rel=1e-12, abs=1e-8
    )

    mask = np.array(
        [[True, False, True, False], [False, True, False, True], [True, False, True, False]]
    )
    expected_area = float(reference[mask].sum(dtype=np.float64))
    expected_percent = expected_area / aoi.area_m2 * 100.0
    measured = measure_mask_coverage(
        mask,
        grid,
        aoi,
        pixel_area_weights=optimized,
    )
    assert measured.area_m2 == pytest.approx(expected_area, rel=1e-12, abs=1e-8)
    assert measured.coverage_percent == pytest.approx(
        expected_percent, abs=1e-8
    )


def test_rotated_grid_uses_complete_affine_geometry_and_documents_legacy_delta(
    tmp_path,
):
    """La rotation/cisaillement suit l’affine complète, contrairement au legacy."""
    geometry = MultiPolygon(
        [
            box(500200, 997200, 501200, 998200),
            box(502800, 1001800, 503800, 1002800),
        ]
    )
    aoi_path = _write_aoi(
        tmp_path / "rotated-aoi.gpkg", [geometry], crs="EPSG:32632"
    )
    aoi = load_equal_area_aoi(aoi_path)
    grid = RasterGrid(
        crs=rasterio.crs.CRS.from_epsg(32632),
        resolution=(1000.0, 1000.0),
        transform=Affine(1000, 200, 500000, 100, -1000, 1003000),
        width=4,
        height=3,
    )

    full_affine_reference = _reference_pixel_area_weights(
        grid, aoi, full_affine=True
    )
    legacy_reference = _reference_pixel_area_weights(grid, aoi)
    optimized = prepare_pixel_area_weights(grid, aoi)

    np.testing.assert_allclose(
        optimized, full_affine_reference, rtol=1e-12, atol=1e-8
    )
    assert not np.allclose(
        full_affine_reference, legacy_reference, rtol=1e-12, atol=1e-8
    )


def test_only_partial_cells_use_exact_intersection(tmp_path, monkeypatch):
    """Les cellules hors AOI et intérieures évitent l’overlay général."""
    from src.geo import change_analysis

    aoi_path = _write_aoi(
        tmp_path / "three-categories.gpkg", [box(0.5, 0.0, 2.5, 1.0)]
    )
    aoi = load_equal_area_aoi(aoi_path)
    grid = RasterGrid(
        crs=rasterio.crs.CRS.from_epsg(6933),
        resolution=(1.0, 1.0),
        transform=Affine(1, 0, 0, 0, -1, 1),
        width=4,
        height=1,
    )
    original_intersection = change_analysis.intersection
    intersection_sizes = []

    def record_intersection(left, right):
        intersection_sizes.append(np.asarray(left, dtype=object).size)
        return original_intersection(left, right)

    monkeypatch.setattr(change_analysis, "intersection", record_intersection)
    weights = prepare_pixel_area_weights(grid, aoi)

    np.testing.assert_allclose(weights, [[0.5, 1.0, 0.5, 0.0]], atol=1e-8)
    assert intersection_sizes == [2]


def test_boundary_touching_exterior_cells_are_not_overlaid(tmp_path, monkeypatch):
    """Un contact de bord sans surface commune reste une cellule extérieure."""
    from src.geo import change_analysis

    aoi_path = _write_aoi(tmp_path / "edge-touch.gpkg", [box(0, 0, 1, 1)])
    aoi = load_equal_area_aoi(aoi_path)
    grid = RasterGrid(
        crs=rasterio.crs.CRS.from_epsg(6933),
        resolution=(1.0, 1.0),
        transform=Affine(1, 0, 0, 0, -1, 1),
        width=3,
        height=1,
    )
    original_intersection = change_analysis.intersection
    intersection_sizes = []

    def record_intersection(left, right):
        intersection_sizes.append(np.asarray(left, dtype=object).size)
        return original_intersection(left, right)

    monkeypatch.setattr(change_analysis, "intersection", record_intersection)
    weights = prepare_pixel_area_weights(grid, aoi)

    np.testing.assert_allclose(weights, [[1.0, 0.0, 0.0]], atol=1e-8)
    assert intersection_sizes == []


def _write_raster(path, values, *, nodata=None):
    values = np.asarray(values)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=values.shape[0],
        width=values.shape[1],
        count=1,
        dtype=values.dtype,
        crs="EPSG:6933",
        transform=from_origin(0, values.shape[0], 1, 1),
        nodata=nodata,
    ) as dataset:
        dataset.write(values, 1)
    return path


def test_analysis_consumes_the_harmonizer_result_without_realigning(tmp_path):
    """L'analyse consomme directement delta et masque produits par le module temporel."""
    ndvi_1 = _write_raster(
        tmp_path / "ndvi-1.tif",
        np.array([[0.3, 0.3]], dtype=np.float32),
        nodata=np.nan,
    )
    ndvi_2 = _write_raster(
        tmp_path / "ndvi-2.tif",
        np.array([[0.5, 0.0]], dtype=np.float32),
        nodata=np.nan,
    )
    scl_1 = _write_raster(
        tmp_path / "scl-1.tif",
        np.array([[4, 4]], dtype=np.uint8),
        nodata=0,
    )
    scl_2 = _write_raster(
        tmp_path / "scl-2.tif",
        np.array([[4, 7]], dtype=np.uint8),
        nodata=0,
    )
    comparison = harmonize_ndvi_pair(ndvi_1, ndvi_2, scl_1, scl_2)

    result = analyze_ndvi_changes(comparison)

    assert result.comparable_mask.tolist() == [[True, False]]
    assert result.change_class_map.tolist() == [[
        ChangeClass.INCREASE,
        ChangeClass.NON_COMPARABLE,
    ]]
    assert result.delta_ndvi[0, 0] == pytest.approx(0.2)


def test_threshold_boundaries_follow_strict_decrease_and_increase_rules():
    """Les égalités ±seuil restent stables ; seules les inégalités strictes changent."""
    comparison = _comparison([[-0.1001, -0.10, 0.10, 0.1001]])

    result = analyze_ndvi_changes(comparison)

    assert result.change_class_map.tolist() == [[
        ChangeClass.DECREASE,
        ChangeClass.STABLE,
        ChangeClass.STABLE,
        ChangeClass.INCREASE,
    ]]


@pytest.mark.parametrize("threshold", [0, -0.1, np.nan, np.inf, True, "invalid"])
def test_threshold_must_be_finite_and_strictly_positive(threshold):
    comparison = _comparison([[0.2]])

    with pytest.raises(ValueError, match="seuil"):
        analyze_ndvi_changes(comparison, threshold=threshold)


def test_statistics_masks_and_surface_areas_conserve_comparable_area():
    """Les classes couvrent exactement les pixels comparables et leurs surfaces."""
    comparison = _comparison(
        [[-0.3, 0.0], [0.3, 0.2]],
        valid=[[True, True], [True, False]],
    )

    result = analyze_ndvi_changes(comparison)

    assert result.statistics["count"] == 3
    assert result.statistics["mean"] == pytest.approx(0.0, abs=1e-7)
    assert result.statistics["min"] == pytest.approx(-0.3)
    assert result.statistics["median"] == pytest.approx(0.0, abs=1e-7)
    assert result.statistics["max"] == pytest.approx(0.3)
    assert result.class_pixel_counts == {"decrease": 1, "stable": 1, "increase": 1}
    assert result.change_class_map[1, 1] == ChangeClass.NON_COMPARABLE
    assert np.isnan(result.delta_ndvi[1, 1])
    assert result.areas_m2["decrease"] == pytest.approx(1.0)
    assert result.areas_m2["stable"] == pytest.approx(1.0)
    assert result.areas_m2["increase"] == pytest.approx(1.0)
    assert result.areas_ha["decrease"] == pytest.approx(0.0001)
    assert result.comparable_area_m2 == pytest.approx(3.0)
    assert sum(result.areas_m2.values()) == pytest.approx(result.comparable_area_m2)
    assert result.coverage_denominator == "comparison_grid"
    assert result.denominator_area_m2 == pytest.approx(4.0)
    assert result.comparable_coverage_percent == pytest.approx(75.0)


def test_aoi_coverage_and_partial_boundary_areas_use_intersections(tmp_path):
    """Les pixels de bord ne contribuent qu'à la fraction intersectant l'AOI."""
    aoi_path = _write_aoi(
        tmp_path / "partial-aoi.gpkg",
        [box(0.5, 0.5, 1.5, 1.5)],
    )
    comparison = _comparison([[-0.3, 0.0], [0.3, 0.0]])

    result = analyze_ndvi_changes(comparison, aoi_path=aoi_path)

    assert result.coverage_denominator == "aoi"
    assert result.denominator_area_m2 == pytest.approx(1.0)
    assert result.comparable_area_m2 == pytest.approx(1.0)
    assert result.comparable_coverage_percent == pytest.approx(100.0)
    assert result.areas_m2["decrease"] == pytest.approx(0.25)
    assert result.areas_m2["increase"] == pytest.approx(0.25)
    assert result.areas_m2["stable"] == pytest.approx(0.5)
    assert sum(result.areas_m2.values()) == pytest.approx(1.0)


def test_aoi_statistics_and_pixel_counts_exclude_exterior_and_include_partial_cells(
    tmp_path,
):
    """Les indicateurs pixels suivent all_touched, les surfaces restent pondérées."""
    aoi_path = _write_aoi(
        tmp_path / "partial-indicators.gpkg", [box(0.25, 0.5, 1.75, 1.5)]
    )
    comparison = _comparison(
        [[-0.2, -0.2, 0.5, 0.5], [-0.2, -0.2, 0.5, 0.5]],
        valid=[[False, True, True, True], [True, True, True, True]],
    )

    result = analyze_ndvi_changes(comparison, aoi_path=aoi_path)

    # Four AOI-touching cells are partial; one is NoData and is excluded.
    assert result.statistics["count"] == 3
    assert result.statistics["mean"] == pytest.approx(-0.2, abs=1e-7)
    assert result.class_pixel_counts == {"decrease": 3, "stable": 0, "increase": 0}
    assert result.areas_m2["decrease"] == pytest.approx(1.125)
    assert result.comparable_area_m2 == pytest.approx(1.125)
    assert result.denominator_area_m2 == pytest.approx(1.5)
    assert result.comparable_coverage_percent == pytest.approx(75.0)
    # The public raster products retain their full georeferenced grid.
    assert result.change_class_map.shape == (2, 4)
    assert result.comparable_mask.tolist() == comparison.comparison_mask.tolist()


def test_aoi_overlaps_are_unioned_and_unobserved_area_lowers_coverage(tmp_path):
    """L'union évite le double compte ; la partie AOI hors comparaison reste au dénominateur."""
    aoi_path = _write_aoi(
        tmp_path / "overlapping-aoi.gpkg",
        [box(0, 0, 1.5, 2), box(0.5, 0, 2, 2)],
    )
    comparison = _comparison(
        [[-0.3, 0.3], [0.0, 0.0]],
        valid=[[True, False], [True, False]],
    )

    result = analyze_ndvi_changes(comparison, aoi_path=aoi_path)

    assert result.denominator_area_m2 == pytest.approx(4.0)
    assert result.comparable_area_m2 == pytest.approx(2.0)
    assert result.comparable_coverage_percent == pytest.approx(50.0)
    assert sum(result.areas_m2.values()) == pytest.approx(2.0)


def test_empty_comparison_returns_zero_area_and_empty_statistics():
    """Sans pixels communs, aucune moyenne n'est fabriquée et les surfaces valent zéro."""
    comparison = _comparison(
        [[0.2, -0.2], [0.1, -0.1]],
        valid=np.zeros((2, 2), dtype=bool),
    )

    result = analyze_ndvi_changes(comparison)

    assert result.statistics["count"] == 0
    assert result.statistics["mean"] is None
    assert result.areas_m2 == {"decrease": 0.0, "stable": 0.0, "increase": 0.0}
    assert result.comparable_area_m2 == 0.0
    assert result.comparable_coverage_percent == 0.0
    assert not result.comparable_mask.any()
    assert np.all(result.change_class_map == ChangeClass.NON_COMPARABLE)


def test_nonfinite_and_out_of_range_ndvi_are_not_comparable():
    """NaN et NDVI hors de [-1, 1] deviennent non comparables, pas des changements."""
    comparison = _comparison(
        [[0.2, 0.2, 0.2]],
        date_1=[[0.3, 0.3, 0.3]],
    )
    comparison.ndvi_date_2[0, 1] = 1.2
    comparison.ndvi_date_2[0, 2] = np.nan

    result = analyze_ndvi_changes(comparison)

    assert result.comparable_mask.tolist() == [[True, False, False]]
    assert result.statistics["count"] == 1
    assert result.excluded_out_of_range_pixels == 1
    assert result.change_class_map.tolist() == [[
        ChangeClass.INCREASE,
        ChangeClass.NON_COMPARABLE,
        ChangeClass.NON_COMPARABLE,
    ]]


def test_geographic_grid_uses_area_not_a_fixed_pixel_size():
    """Une grille longitude/latitude produit des aires réelles en m², pas 100 m²/cellule."""
    comparison = _comparison(
        [[0.2, -0.2], [0.0, 0.0]],
        transform=Affine(0.01, 0, 8.7, 0, -0.01, 0.05),
        crs="EPSG:4326",
    )

    result = analyze_ndvi_changes(comparison)

    assert result.comparable_area_m2 > 4_000_000
    assert result.denominator_area_m2 == pytest.approx(result.comparable_area_m2)
    assert sum(result.areas_m2.values()) == pytest.approx(
        result.comparable_area_m2,
        rel=1e-9,
    )
