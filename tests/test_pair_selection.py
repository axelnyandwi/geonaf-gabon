"""Synthetic tests for STAC completeness and common-SCL pair ranking."""

import geopandas as gpd
import numpy as np
import pytest
import rasterio
from affine import Affine
from rasterio.transform import from_origin
from rasterio.transform import from_bounds
from rasterio.warp import transform_bounds
from shapely.affinity import affine_transform as apply_affine
from shapely.geometry import box

from src.data.ingestion import STACSearchResults
from src.geo.pair_selection import (
    IncompleteSTACSearchError,
    PairSelectionConfig,
    PairSearchScope,
    PairSuitability,
    _crop_grid_to_aoi_bounds,
    select_temporal_pairs,
)
from src.geo.temporal import RasterGrid


@pytest.fixture
def aoi_path(tmp_path):
    path = tmp_path / "aoi.gpkg"
    gpd.GeoDataFrame({"geometry": [box(0, 0, 4, 4)]}, crs="EPSG:6933").to_file(
        path, driver="GPKG"
    )
    return path


def _write_scl(path, values, *, valid_mask=None, transform=None, crs="EPSG:6933"):
    values = np.asarray(values, dtype=np.uint8)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=values.shape[1],
        height=values.shape[0],
        count=1,
        dtype="uint8",
        crs=crs,
        transform=transform or from_origin(0, 4, 1, 1),
        nodata=0,
    ) as dataset:
        dataset.write(values, 1)
        if valid_mask is not None:
            dataset.write_mask(np.asarray(valid_mask, dtype=np.uint8) * 255)
    return path


def _item(product_id, day, *, cloud=10, datetime_value=None):
    return {
        "id": product_id,
        "properties": {
            "datetime": datetime_value or f"2025-01-{day:02d}T10:00:00Z",
            "eo:cloud_cover": cloud,
        },
    }


def _quality(usable=80, coverage=100, data_coverage=100):
    return {
        "usable_land_percent": usable,
        "aoi_coverage_percent": coverage,
        "aoi_data_coverage_percent": data_coverage,
    }


def _aoi_frame(geometry, crs="EPSG:6933"):
    return gpd.GeoDataFrame({"geometry": [geometry]}, crs=crs)


def test_crop_north_up_grid_preserves_legacy_window_and_transform():
    grid = RasterGrid(
        crs=rasterio.crs.CRS.from_epsg(6933),
        resolution=(1.0, 1.0),
        transform=from_origin(0, 4, 1, 1),
        width=4,
        height=4,
    )

    cropped = _crop_grid_to_aoi_bounds(
        grid, _aoi_frame(box(0.5, 0.5, 2.5, 2.5))
    )

    assert cropped is not None
    assert (cropped.width, cropped.height) == (3, 3)
    assert cropped.crs == grid.crs
    assert cropped.resolution == grid.resolution
    assert cropped.transform == grid.transform @ Affine.translation(0, 1)


@pytest.mark.parametrize(
    "transform",
    [
        Affine(2, -0.5, 100, 0.4, -3, 500),
        Affine(2, 0.6, 100, 0.35, -3, 500),
    ],
    ids=["rotated", "sheared"],
)
def test_crop_rotated_and_sheared_grids_uses_inverse_affine(transform):
    grid = RasterGrid(
        crs=rasterio.crs.CRS.from_epsg(6933),
        resolution=(abs(transform.a), abs(transform.e)),
        transform=transform,
        width=10,
        height=8,
    )
    pixel_aoi = box(1.25, 2.25, 5.75, 6.2)
    world_aoi = apply_affine(
        pixel_aoi,
        [
            transform.a,
            transform.b,
            transform.d,
            transform.e,
            transform.c,
            transform.f,
        ],
    )

    cropped = _crop_grid_to_aoi_bounds(grid, _aoi_frame(world_aoi))

    assert cropped is not None
    assert cropped.crs == grid.crs
    assert cropped.resolution == grid.resolution
    assert cropped.width >= 5 and cropped.height >= 5
    assert cropped.transform == grid.transform @ Affine.translation(1, 2)


def test_crop_transforms_aoi_crs_before_inverse_affine():
    grid = RasterGrid(
        crs=rasterio.crs.CRS.from_epsg(3857),
        resolution=(1.0, 1.0),
        transform=from_origin(0, 5, 1, 1),
        width=5,
        height=5,
    )
    # Approximately 1–4 metres from the origin after reprojection to Web Mercator.
    frame = _aoi_frame(box(0.000009, 0.000009, 0.000027, 0.000027), "EPSG:4326")

    cropped = _crop_grid_to_aoi_bounds(grid, frame)

    assert cropped is not None
    assert (cropped.width, cropped.height) == (3, 3)
    assert cropped.transform == grid.transform @ Affine.translation(1, 1)


def test_crop_clips_partially_external_aoi_without_dropping_overlap():
    grid = RasterGrid(
        crs=rasterio.crs.CRS.from_epsg(6933),
        resolution=(1.0, 1.0),
        transform=from_origin(0, 4, 1, 1),
        width=4,
        height=4,
    )

    cropped = _crop_grid_to_aoi_bounds(
        grid, _aoi_frame(box(-1.5, 2.25, 1.25, 5.5))
    )

    assert cropped is not None
    assert (cropped.width, cropped.height) == (2, 2)
    assert cropped.transform == grid.transform


def test_crop_returns_none_when_aoi_is_disjoint_or_only_touches_grid():
    grid = RasterGrid(
        crs=rasterio.crs.CRS.from_epsg(6933),
        resolution=(1.0, 1.0),
        transform=from_origin(0, 4, 1, 1),
        width=4,
        height=4,
    )

    assert _crop_grid_to_aoi_bounds(grid, _aoi_frame(box(10, 10, 11, 11))) is None
    assert _crop_grid_to_aoi_bounds(grid, _aoi_frame(box(4, 1, 5, 2))) is None


def _search(items):
    return STACSearchResults(items, truncated=False, raw_count=len(items))


def test_rank_uses_actual_common_scl_mask_then_deterministic_quality_tie_break(
    tmp_path, aoi_path
):
    first = np.full((4, 4), 7, dtype=np.uint8)
    first[:, :3] = 4
    second = np.full((4, 4), 7, dtype=np.uint8)
    second[:, 1:] = 4
    full = np.full((4, 4), 4, dtype=np.uint8)
    paths = {
        "A": _write_scl(tmp_path / "a.tif", first),
        "B": _write_scl(tmp_path / "b.tif", second),
        "C": _write_scl(tmp_path / "c.tif", full),
    }
    items = [_item("A", 1), _item("B", 11), _item("C", 21)]

    result = select_temporal_pairs(
        _search(items),
        scl_paths=paths,
        quality_by_product={
            "A": _quality(90),
            "B": _quality(50),
            "C": _quality(75),
        },
        aoi_path=aoi_path,
    )

    assert result.ranking_is_complete
    assert result.evaluated_pairs == 3
    # 75% is the raster-mask intersection. Individual quality scores do not
    # multiply or stand in for pairwise coverage.
    assert result.pairs[0].earlier_product_id == "A"
    assert result.pairs[0].later_product_id == "C"
    assert result.pairs[0].common_coverage_percent == pytest.approx(75)
    pair_ab = next(pair for pair in result.pairs if (pair.earlier_product_id, pair.later_product_id) == ("A", "B"))
    assert pair_ab.common_coverage_percent == pytest.approx(50)
    assert result.pairs[0].suitability is PairSuitability.POTENTIALLY_REPRESENTATIVE


def test_scl_nodata_and_internal_mask_exclude_nonzero_pixels(tmp_path, aoi_path):
    values = np.full((4, 4), 4, dtype=np.uint8)
    validity = np.ones((4, 4), dtype=bool)
    validity[0, 0] = False  # Hidden nonzero SCL value must remain invalid.
    second_values = values.copy()
    second_values[0, 1] = 0
    paths = {
        "A": _write_scl(tmp_path / "a.tif", values, valid_mask=validity),
        "B": _write_scl(tmp_path / "b.tif", second_values),
    }

    result = select_temporal_pairs(
        _search([_item("A", 1), _item("B", 11)]),
        scl_paths=paths,
        quality_by_product={"A": _quality(), "B": _quality()},
        aoi_path=aoi_path,
        config=PairSelectionConfig(exploratory_coverage_percent=0),
    )

    assert result.pairs[0].common_usable_pixels == 14
    assert result.pairs[0].common_coverage_percent == pytest.approx(87.5)


def test_partial_aoi_boundary_cells_are_area_weighted(tmp_path):
    aoi = tmp_path / "partial-aoi.gpkg"
    gpd.GeoDataFrame({"geometry": [box(0.5, 0.5, 1.5, 1.5)]}, crs="EPSG:6933").to_file(
        aoi, driver="GPKG"
    )
    values = np.full((2, 2), 4, dtype=np.uint8)
    paths = {
        "A": _write_scl(tmp_path / "a.tif", values, transform=from_origin(0, 2, 1, 1)),
        "B": _write_scl(tmp_path / "b.tif", values, transform=from_origin(0, 2, 1, 1)),
    }

    result = select_temporal_pairs(
        _search([_item("A", 1), _item("B", 11)]),
        scl_paths=paths,
        quality_by_product={"A": _quality(), "B": _quality()},
        aoi_path=aoi,
    )

    pair = result.pairs[0]
    assert pair.common_area_m2 == pytest.approx(1.0)
    assert pair.common_coverage_percent == pytest.approx(100.0)
    assert pair.common_usable_pixels == 4


def test_interval_and_cheap_quality_filters_run_before_raster_pairs(tmp_path, aoi_path):
    values = np.full((4, 4), 4, dtype=np.uint8)
    paths = {key: _write_scl(tmp_path / f"{key}.tif", values) for key in "ABC"}
    items = [_item("A", 1, cloud=5), _item("B", 3, cloud=80), _item("C", 21, cloud=5)]

    result = select_temporal_pairs(
        _search(items),
        scl_paths=paths,
        quality_by_product={"A": _quality(), "B": _quality(), "C": _quality()},
        aoi_path=aoi_path,
        config=PairSelectionConfig(
            min_interval_days=10,
            max_interval_days=25,
            max_cloud_cover_percent=20,
        ),
    )

    assert result.eligible_acquisitions == 2
    assert result.rejected_counts["cloud_filter"] == 1
    assert len(result.pairs) == 1
    assert result.pairs[0].interval_days == pytest.approx(20)


def test_duplicate_products_are_counted_once(tmp_path, aoi_path):
    values = np.full((4, 4), 4, dtype=np.uint8)
    paths = {
        "A": _write_scl(tmp_path / "a.tif", values),
        "B": _write_scl(tmp_path / "b.tif", values),
    }
    items = [_item("A", 1), _item("A", 1), _item("B", 11)]

    result = select_temporal_pairs(
        _search(items),
        scl_paths=paths,
        quality_by_product={"A": _quality(), "B": _quality()},
        aoi_path=aoi_path,
    )

    assert result.eligible_acquisitions == 2
    assert result.rejected_counts["duplicate_product"] == 1
    assert len(result.pairs) == 1


def test_truncated_or_unverified_stac_search_is_never_accepted(tmp_path, aoi_path):
    truncated = STACSearchResults([_item("A", 1)], truncated=True, raw_count=2)

    with pytest.raises(IncompleteSTACSearchError, match="tronquée"):
        select_temporal_pairs(
            truncated,
            scl_paths={},
            quality_by_product={},
            aoi_path=aoi_path,
        )
    with pytest.raises(IncompleteSTACSearchError, match="explicitement"):
        select_temporal_pairs(
            [_item("A", 1)],
            scl_paths={},
            quality_by_product={},
            aoi_path=aoi_path,
        )


def test_plain_item_list_requires_explicit_exhaustiveness(tmp_path, aoi_path):
    values = np.full((4, 4), 4, dtype=np.uint8)
    paths = {key: _write_scl(tmp_path / f"{key}.tif", values) for key in "AB"}

    result = select_temporal_pairs(
        [_item("A", 1), _item("B", 11)],
        search_complete=True,
        scl_paths=paths,
        quality_by_product={"A": _quality(), "B": _quality()},
        aoi_path=aoi_path,
    )

    assert len(result.pairs) == 1


def test_local_subset_mode_is_explicit_and_does_not_claim_stac_completeness(
    tmp_path, aoi_path
):
    values = np.full((4, 4), 4, dtype=np.uint8)
    paths = {key: _write_scl(tmp_path / f"{key}.tif", values) for key in "AB"}
    result = select_temporal_pairs(
        [_item("A", 1), _item("B", 11)],
        search_scope="local_subset",
        scl_paths=paths,
        quality_by_product={"A": _quality(), "B": _quality()},
        aoi_path=aoi_path,
    )

    assert result.search_scope is PairSearchScope.LOCAL_SUBSET
    assert result.ranking_is_complete  # Complete only within this local subset.
    assert len(result.pairs) == 1

    with pytest.raises(ValueError, match="Ne pas déclarer search_complete=True"):
        select_temporal_pairs(
            [_item("A", 1)],
            search_scope="local_subset",
            search_complete=True,
            scl_paths={},
            quality_by_product={},
            aoi_path=aoi_path,
        )


def test_local_subset_mode_never_accepts_truncated_stac_results(aoi_path):
    with pytest.raises(IncompleteSTACSearchError, match="tronquée"):
        select_temporal_pairs(
            STACSearchResults([_item("A", 1)], truncated=True, raw_count=2),
            search_scope="local_subset",
            scl_paths={},
            quality_by_product={},
            aoi_path=aoi_path,
        )


def test_incomplete_metadata_quality_or_local_raster_is_rejected(tmp_path, aoi_path):
    values = np.full((4, 4), 4, dtype=np.uint8)
    paths = {"A": _write_scl(tmp_path / "a.tif", values), "B": tmp_path / "missing.tif"}
    missing_date = _item("C", 21)
    del missing_date["properties"]["datetime"]
    paths["E"] = _write_scl(tmp_path / "e.tif", values)
    items = [
        _item("A", 1),
        _item("B", 11),
        missing_date,
        _item("E", 27),
    ]

    result = select_temporal_pairs(
        _search(items),
        scl_paths=paths,
        quality_by_product={"A": _quality()},
        aoi_path=aoi_path,
    )

    assert result.eligible_acquisitions == 1
    assert result.rejected_counts["incomplete_local_data"] == 1
    assert result.rejected_counts["incomplete_quality"] == 1
    assert result.rejected_counts["incomplete_metadata"] == 1
    assert result.pairs == ()


def test_empty_pair_set_and_interval_rejection_are_explicit(tmp_path, aoi_path):
    values = np.full((4, 4), 4, dtype=np.uint8)
    paths = {key: _write_scl(tmp_path / f"{key}.tif", values) for key in "AB"}

    result = select_temporal_pairs(
        _search([_item("A", 1), _item("B", 2)]),
        scl_paths=paths,
        quality_by_product={"A": _quality(), "B": _quality()},
        aoi_path=aoi_path,
        config=PairSelectionConfig(min_interval_days=5),
    )

    assert result.pairs == ()
    assert result.rejected_counts["date_filter"] == 1


def test_spatially_disjoint_scl_footprints_produce_no_pair(tmp_path, aoi_path):
    values = np.full((4, 4), 4, dtype=np.uint8)
    paths = {
        "A": _write_scl(tmp_path / "a.tif", values),
        "B": _write_scl(
            tmp_path / "b.tif",
            values,
            transform=from_origin(10, 14, 1, 1),
        ),
    }

    result = select_temporal_pairs(
        _search([_item("A", 1), _item("B", 11)]),
        scl_paths=paths,
        quality_by_product={"A": _quality(), "B": _quality()},
        aoi_path=aoi_path,
    )

    assert result.pairs == ()
    assert result.rejected_counts["no_raster_intersection"] == 1


def test_pair_limits_are_reported_and_ranking_marked_incomplete(tmp_path, aoi_path):
    values = np.full((4, 4), 4, dtype=np.uint8)
    paths = {key: _write_scl(tmp_path / f"{key}.tif", values) for key in "ABC"}
    items = [_item("A", 1), _item("B", 11), _item("C", 21)]

    result = select_temporal_pairs(
        _search(items),
        scl_paths=paths,
        quality_by_product={key: _quality() for key in "ABC"},
        aoi_path=aoi_path,
        config=PairSelectionConfig(max_pair_evaluations=1),
    )

    assert result.evaluated_pairs == 1
    assert result.pair_limit_reached
    assert not result.ranking_is_complete


def test_reuses_each_aligned_mask_and_equal_area_weights(tmp_path, aoi_path, monkeypatch):
    """Chaque SCL et les aires AOI sont calculés une seule fois sur une grille partagée."""
    from src.geo import pair_selection

    values = np.full((4, 4), 4, dtype=np.uint8)
    paths = {key: _write_scl(tmp_path / f"{key}.tif", values) for key in "ABC"}
    mask_calls = 0
    weight_calls = 0
    original_mask = pair_selection.scl_policy_mask_on_grid
    original_weights = pair_selection.prepare_pixel_area_weights

    def count_mask(*args, **kwargs):
        nonlocal mask_calls
        mask_calls += 1
        return original_mask(*args, **kwargs)

    def count_weights(*args, **kwargs):
        nonlocal weight_calls
        weight_calls += 1
        return original_weights(*args, **kwargs)

    monkeypatch.setattr(pair_selection, "scl_policy_mask_on_grid", count_mask)
    monkeypatch.setattr(pair_selection, "prepare_pixel_area_weights", count_weights)
    result = select_temporal_pairs(
        _search([_item("A", 1), _item("B", 11), _item("C", 21)]),
        scl_paths=paths,
        quality_by_product={key: _quality() for key in "ABC"},
        aoi_path=aoi_path,
    )

    assert result.evaluated_pairs == 3
    assert len(result.pairs) == 3
    assert mask_calls == 3
    assert weight_calls == 1


def test_equal_coverage_ties_use_quality_cloud_interval_then_product_ids(
    tmp_path, aoi_path
):
    """Les ex aequo de couverture suivent les départages configurés, sans hasard."""
    values = np.full((4, 4), 4, dtype=np.uint8)
    paths = {
        key: _write_scl(tmp_path / f"{key}.tif", values) for key in "ABCD"
    }
    items = [
        _item("A", 1, cloud=10),
        _item("B", 11, cloud=10),
        _item("C", 21, cloud=20),
        _item("D", 31, cloud=20),
    ]

    result = select_temporal_pairs(
        _search(items),
        scl_paths=paths,
        quality_by_product={
            "A": _quality(80),
            "B": _quality(80),
            "C": _quality(60),
            "D": _quality(60),
        },
        aoi_path=aoi_path,
    )

    assert len(result.pairs) == 6
    assert {pair.common_coverage_percent for pair in result.pairs} == {100.0}
    assert [
        (pair.earlier_product_id, pair.later_product_id) for pair in result.pairs
    ] == [
        ("A", "B"),  # score terrain exploitable supérieur
        ("B", "C"),  # nuages égaux, intervalle plus court
        ("A", "C"),  # mêmes scores et intervalle que B-D, identifiants
        ("B", "D"),
        ("A", "D"),  # intervalle plus long
        ("C", "D"),
    ]


def test_acquisition_limit_is_reported(tmp_path, aoi_path):
    values = np.full((4, 4), 4, dtype=np.uint8)
    paths = {key: _write_scl(tmp_path / f"{key}.tif", values) for key in "ABC"}
    result = select_temporal_pairs(
        _search([_item("A", 1), _item("B", 11), _item("C", 21)]),
        scl_paths=paths,
        quality_by_product={key: _quality() for key in "ABC"},
        aoi_path=aoi_path,
        config=PairSelectionConfig(max_acquisitions=2),
    )

    assert result.eligible_acquisitions == 3
    assert result.acquisition_limit_reached
    assert result.evaluated_pairs == 1
    assert not result.ranking_is_complete


def test_different_crs_scl_is_aligned_for_common_coverage(tmp_path):
    aoi = tmp_path / "metric-aoi.gpkg"
    gpd.GeoDataFrame({"geometry": [box(0, 0, 2000, 2000)]}, crs="EPSG:6933").to_file(
        aoi, driver="GPKG"
    )
    metric_values = np.full((20, 20), 4, dtype=np.uint8)
    geographic_bounds = transform_bounds(
        "EPSG:6933", "EPSG:4326", 0, 0, 2000, 2000, densify_pts=21
    )
    geographic_transform = from_bounds(*geographic_bounds, 20, 20)
    paths = {
        "A": _write_scl(
            tmp_path / "metric.tif",
            metric_values,
            transform=from_origin(0, 2000, 100, 100),
            crs="EPSG:6933",
        ),
        "B": _write_scl(
            tmp_path / "geographic.tif",
            metric_values,
            transform=geographic_transform,
            crs="EPSG:4326",
        ),
    }

    result = select_temporal_pairs(
        _search([_item("A", 1), _item("B", 11)]),
        scl_paths=paths,
        quality_by_product={"A": _quality(), "B": _quality()},
        aoi_path=aoi,
        config=PairSelectionConfig(exploratory_coverage_percent=0),
    )

    assert len(result.pairs) == 1
    assert result.pairs[0].common_coverage_percent > 80


def test_pair_suitability_thresholds_are_configurable(tmp_path, aoi_path):
    partial = np.full((4, 4), 7, dtype=np.uint8)
    partial[:, :2] = 4
    paths = {
        "A": _write_scl(tmp_path / "a.tif", partial),
        "B": _write_scl(tmp_path / "b.tif", np.full((4, 4), 4, dtype=np.uint8)),
    }
    result = select_temporal_pairs(
        _search([_item("A", 1), _item("B", 11)]),
        scl_paths=paths,
        quality_by_product={"A": _quality(), "B": _quality()},
        aoi_path=aoi_path,
        config=PairSelectionConfig(
            exploratory_coverage_percent=60,
            representative_coverage_percent=90,
        ),
    )

    assert result.pairs[0].common_coverage_percent == pytest.approx(50)
    assert result.pairs[0].suitability is PairSuitability.INSUFFICIENT


def test_quality_filters_require_valid_percentages(tmp_path, aoi_path):
    values = np.full((4, 4), 4, dtype=np.uint8)
    paths = {key: _write_scl(tmp_path / f"{key}.tif", values) for key in "AB"}

    with pytest.raises(ValueError, match="min_aoi_coverage_percent"):
        PairSelectionConfig(min_aoi_coverage_percent=101)

    result = select_temporal_pairs(
        _search([_item("A", 1), _item("B", 11)]),
        scl_paths=paths,
        quality_by_product={"A": _quality(usable=np.nan), "B": _quality()},
        aoi_path=aoi_path,
    )
    assert result.pairs == ()
    assert result.rejected_counts["incomplete_quality"] == 1
