"""Experimental Sentinel-2 vegetation estimate: profile, SCL alignment, partition, areas, quality; synthetic, offline."""

from dataclasses import replace
from types import SimpleNamespace

import geopandas as gpd
import numpy as np
import pytest
import rasterio
from rasterio.crs import CRS
from rasterio.transform import from_origin
from shapely.geometry import box

from src.data.cloud_mask import SCL_MASK_POLICIES, scl_mask_from_values
from src.geo.change_analysis import load_equal_area_aoi, prepare_pixel_area_weights
from src.geo.scientific_profile import GABON_SENSITIVITY_EXPERIMENTAL, REFERENCE_PROFILES
from src.geo.temporal import RasterGrid, scl_policy_mask_on_grid, scl_values_on_grid
from src.indicators.profiles import (
    AFRICA_DEFAULT_V1, EXPERIMENTAL, S2_NDVI_VEGETATION_V0, VALIDATED, VegetationIndexProfile,
)
from src.indicators.quality import vegetation_estimate_assessment
from src.indicators.vegetation_s2 import (
    EXCLUDED_OBSERVED, LABEL_NAMES, NOT_OBSERVED, NOT_VEGETATED, OTHER_INVALID, OUTSIDE_FOOTPRINT, UNSPLIT,
    VEGETATED, measure_observed_vegetation, measure_vegetation, partition_labels,
)
from tests.test_indicators_modules import AFRICA_DEFAULT_V1_FINGERPRINT

X0, Y0 = 500_000.0, 100_000.0          # UTM 32N, near the equator (Gabon).
F32_THRESHOLD = np.float32(0.4)
SCIENTIFIC_FINGERPRINTS = {             # Unchanged by L3a (28/10/2026).
    "akanda": "c78ff691b61d9f7701981c8071954a68e2878669f0919e3187ff641248af207a",
    "libreville": "6fe8908316c71ba47a71cd672ad68842976f8b561afe183d42f275b423eaa4df",
    "owendo": "f289c7ef69cab27362599b7fc0d1059c003a729798a6b2837cc2b9893467f294",
}


def make_grid(width, height, x0=X0, y0=Y0, epsg=32632, res=10.0):
    return RasterGrid(CRS.from_epsg(epsg), (res, res), from_origin(x0, y0 + height * res, res, res), width, height)


def zone_for(tmp_path, geometry, epsg=32632, name="zone"):
    path = tmp_path / f"{name}.geojson"
    gpd.GeoDataFrame(geometry=[geometry], crs=f"EPSG:{epsg}").to_file(path)
    zone = load_equal_area_aoi(path)
    return zone


def measure(grid, zone, ndvi, valid=None, *, scl=None, threshold=0.4, policy="usable_land", sensitivity=()):
    valid = np.ones(ndvi.shape, bool) if valid is None else valid
    weights = prepare_pixel_area_weights(grid, zone)
    return measure_vegetation(ndvi.astype(np.float32), valid, weights, grid, zone, threshold=threshold,
                              policy=policy, scl=scl, sensitivity=sensitivity)


def parts_sum(result):
    parts = (result.vegetated_m2, result.not_vegetated_m2, result.outside_footprint_m2, result.not_observed_m2,
             result.excluded_observed_m2, result.other_invalid_m2, result.unsplit_m2)
    return sum(part for part in parts if part is not None)


# --- Profile ---------------------------------------------------------------------------------------------


def test_experimental_profile_is_versioned_and_leaves_existing_fingerprints_alone():
    profile = S2_NDVI_VEGETATION_V0
    assert (profile.threshold, profile.min_observed_coverage_percent) == (0.4, 30.0)
    assert profile.scientific_status == EXPERIMENTAL and profile.reliability_cap == "faible"
    assert profile.sensitivity_thresholds == (0.3, 0.4, 0.5)
    assert replace(profile, threshold=0.5).fingerprint != profile.fingerprint
    assert replace(profile, min_observed_coverage_percent=50.0).fingerprint != profile.fingerprint
    assert AFRICA_DEFAULT_V1.fingerprint == AFRICA_DEFAULT_V1_FINGERPRINT
    assert {key: p.fingerprint for key, p in REFERENCE_PROFILES.items()} == SCIENTIFIC_FINGERPRINTS
    assert GABON_SENSITIVITY_EXPERIMENTAL.change_threshold == 0.15   # Untouched scientific scenario.


@pytest.mark.parametrize("changes", [
    {"threshold": 1.5}, {"threshold": float("nan")}, {"threshold": True}, {"sensitivity_thresholds": (0.3, 0.5)},
    {"min_observed_coverage_percent": 120.0}, {"scientific_status": "proven"}, {"reliability_cap": "parfaite"},
    {"index": "EVI"}, {"rationale": " "},
], ids=lambda c: next(iter(c)))
def test_invalid_vegetation_profiles_are_refused(changes):
    with pytest.raises(ValueError):
        replace(S2_NDVI_VEGETATION_V0, **changes)


# --- SCL classes on a grid ----------------------------------------------------------------------------------


def write_scl(path, classes, x0=X0, y0=Y0, res=20.0):
    height, width = classes.shape
    with rasterio.open(path, "w", driver="GTiff", width=width, height=height, count=1, dtype="uint8", nodata=0,
                       crs="EPSG:32632", transform=from_origin(x0, y0 + height * res, res, res)) as target:
        target.write(classes.astype(np.uint8), 1)
    return path


def test_scl_classes_are_aligned_by_nearest_neighbour_and_nodata_stays_invalid(tmp_path):
    classes = np.array([[4, 5, 6], [8, 0, 3], [9, 10, 2]], dtype=np.uint8)
    path = write_scl(tmp_path / "scl.tif", classes)
    fine = make_grid(8, 6, res=10.0)          # 10 m, one extra 20 m column outside the SCL footprint.
    values, valid = scl_values_on_grid(path, fine)
    assert values.dtype == np.uint8 and values.shape == valid.shape == (6, 8)
    np.testing.assert_array_equal(values[:, :6], np.kron(classes, np.ones((2, 2), np.uint8)))
    assert not valid[:, 6:].any() and (values[:, 6:] == 0).all()     # Outside the source footprint.
    assert not valid[2:4, 2:4].any()                                  # Source NoData (class 0).
    same, same_valid = scl_values_on_grid(path, make_grid(3, 3, res=20.0))
    np.testing.assert_array_equal(same, classes)
    np.testing.assert_array_equal(same_valid, classes != 0)


@pytest.mark.parametrize("policy", sorted(SCL_MASK_POLICIES))
def test_policy_masks_are_the_classes_read_by_the_shared_helper(tmp_path, policy):
    path = write_scl(tmp_path / "scl.tif", np.arange(12, dtype=np.uint8).reshape(3, 4))
    grid = make_grid(9, 6, res=10.0)
    values, valid = scl_values_on_grid(path, grid)
    expected = scl_mask_from_values(np.ma.array(values, mask=~valid), policy=policy, valid_mask=valid)
    np.testing.assert_array_equal(scl_policy_mask_on_grid(path, grid, policy), expected)


# --- Partition ----------------------------------------------------------------------------------------------


def test_threshold_equality_is_included_in_float32():
    ndvi = np.array([[F32_THRESHOLD, np.nextafter(F32_THRESHOLD, np.float32(-1))]], dtype=np.float32)
    labels = partition_labels(ndvi, np.ones(ndvi.shape, bool), 0.4, "usable_land")
    assert labels.tolist() == [[VEGETATED, NOT_VEGETATED]]


def test_ndvi_must_be_float32(tmp_path):
    grid = make_grid(2, 2)
    zone = zone_for(tmp_path, box(X0, Y0, X0 + 20, Y0 + 20))
    with pytest.raises(TypeError, match="float32"):
        measure_vegetation(np.full((2, 2), 0.5), np.ones((2, 2), bool), prepare_pixel_area_weights(grid, zone),
                           grid, zone, threshold=0.4, policy="usable_land")


def test_each_scl_class_gets_its_category():
    # Classes 0–11, then class 4 with SCL NoData, then class 4 with an invalid reflectance (NaN NDVI).
    classes = np.array([[0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 4, 4]], dtype=np.uint8)
    scl_valid = np.ones(classes.shape, bool)
    scl_valid[0, 12] = False
    ndvi = np.full(classes.shape, 0.8, np.float32)
    ndvi[0, 5], ndvi[0, 13] = 0.1, np.nan
    raster_ok = np.isfinite(ndvi)
    valid = scl_mask_from_values(np.ma.array(classes, mask=~scl_valid), "usable_land", scl_valid) & raster_ok
    labels = partition_labels(ndvi, valid, 0.4, "usable_land", (classes, scl_valid))
    assert labels[0].tolist() == [
        OUTSIDE_FOOTPRINT, NOT_OBSERVED, EXCLUDED_OBSERVED, NOT_OBSERVED, VEGETATED, NOT_VEGETATED,
        EXCLUDED_OBSERVED, EXCLUDED_OBSERVED, NOT_OBSERVED, NOT_OBSERVED, NOT_OBSERVED, EXCLUDED_OBSERVED,
        OUTSIDE_FOOTPRINT, OTHER_INVALID]


@pytest.mark.parametrize("with_scl", [True, False], ids=["SCL fourni", "SCL absent"])
def test_partition_is_exclusive_and_exhaustive_on_random_inputs(tmp_path, with_scl):
    rng = np.random.default_rng(20261028)
    grid = make_grid(40, 30)
    zone = zone_for(tmp_path, box(X0 + 3, Y0 - 50, X0 + 333, Y0 + 247))   # Partial pixels, part off the grid.
    classes = rng.integers(0, 12, grid.height * grid.width).astype(np.uint8).reshape(30, 40)
    scl_valid = rng.random(classes.shape) > 0.1
    ndvi = rng.uniform(-1, 1, classes.shape).astype(np.float32)
    ndvi[rng.random(classes.shape) < 0.05] = np.nan
    valid = (scl_mask_from_values(np.ma.array(classes, mask=~scl_valid), "usable_land", scl_valid)
             & np.isfinite(ndvi))
    scl = (classes, scl_valid) if with_scl else None
    labels = partition_labels(ndvi, valid, 0.4, "usable_land", scl)
    allowed = set(LABEL_NAMES) - ({UNSPLIT} if with_scl else {OUTSIDE_FOOTPRINT, NOT_OBSERVED, EXCLUDED_OBSERVED,
                                                              OTHER_INVALID})
    assert set(np.unique(labels)) <= allowed and labels.shape == classes.shape   # One label per pixel.
    np.testing.assert_array_equal(np.isin(labels, (VEGETATED, NOT_VEGETATED)), valid)
    result = measure(grid, zone, ndvi, valid, scl=scl)
    assert parts_sum(result) == pytest.approx(result.zone_m2, rel=1e-9)
    assert result.vegetated_m2 + result.not_vegetated_m2 == pytest.approx(result.observed_m2, rel=1e-12)
    assert result.outside_footprint_m2 > 0                                     # Zone partly off the grid.
    assert (result.unsplit_m2 is None) == with_scl and (result.not_observed_m2 is None) != with_scl


# --- Areas, partial pixels, units ---------------------------------------------------------------------------


def layout(tmp_path, epsg, x0, y0):
    """4 × 4 grid; zone offset by half a pixel: inner 2 × 2 full (400 m²), 8 halves, 4 quarters (900 m²)."""
    grid = make_grid(4, 4, x0=x0, y0=y0, epsg=epsg)
    zone = zone_for(tmp_path, box(x0 + 5, y0 + 5, x0 + 35, y0 + 35), epsg=epsg, name=f"zone{epsg}")
    ndvi = np.full((4, 4), 0.1, np.float32)
    ndvi[[0, 0, 3, 3], [0, 3, 0, 3]] = 0.9                          # Vegetation on the four corner pixels.
    return grid, zone, ndvi


@pytest.mark.parametrize("epsg, x0, y0", [(32632, X0, Y0), (32735, X0, 8_700_000.0)], ids=["UTM 32N", "UTM 35S"])
def test_partial_pixels_count_for_their_part_inside_the_zone(tmp_path, epsg, x0, y0):
    grid, zone, ndvi = layout(tmp_path, epsg, x0, y0)
    result = measure(grid, zone, ndvi)
    # Equal-area (EPSG:6933) versus UTM plane areas: the UTM scale factor stays below 1e-3 here.
    assert result.zone_m2 == pytest.approx(900.0, rel=2e-3)
    assert result.observed_m2 == pytest.approx(900.0, rel=2e-3)
    assert result.vegetated_m2 == pytest.approx(4 * 25.0, rel=2e-3)    # Four quarter pixels.
    assert result.vegetated_share_percent == pytest.approx(100 * 100 / 900, rel=1e-6)
    assert result.observed_coverage_percent == pytest.approx(100.0, rel=1e-9)
    assert result.vegetated_pixels == 4 and result.observed_pixels == 16
    assert result.resolution == (10.0, 10.0) and str(epsg) in result.crs


def test_zone_beyond_the_grid_is_outside_the_footprint(tmp_path):
    grid = make_grid(2, 2)
    zone = zone_for(tmp_path, box(X0, Y0, X0 + 40, Y0 + 20))          # Grid covers half the zone.
    result = measure(grid, zone, np.full((2, 2), 0.9, np.float32))
    assert result.outside_footprint_m2 == pytest.approx(400.0, rel=2e-3)
    assert result.observed_coverage_percent == pytest.approx(50.0, rel=1e-6)
    assert result.vegetated_share_percent == pytest.approx(100.0)


def test_sensitivity_thresholds_are_measured_on_the_same_observed_area(tmp_path):
    grid = make_grid(20, 20)
    zone = zone_for(tmp_path, box(X0, Y0, X0 + 200, Y0 + 200))
    ndvi = np.random.default_rng(7).uniform(-0.2, 0.9, (20, 20)).astype(np.float32)
    result = measure(grid, zone, ndvi, sensitivity=(0.3, 0.5))
    areas = dict(result.sensitivity_m2)
    assert list(areas) == [0.3, 0.4, 0.5] and areas[0.3] >= areas[0.4] >= areas[0.5]
    assert areas[0.4] == result.vegetated_m2
    assert all(area <= result.observed_m2 for area in areas.values())


def test_wrapper_reads_an_ndvi_observation(tmp_path):
    grid, zone, ndvi = layout(tmp_path, 32632, X0, Y0)
    observation = SimpleNamespace(ndvi=ndvi, valid=np.ones((4, 4), bool), grid=grid, scl_policy="usable_land",
                                  pixel_area_weights=prepare_pixel_area_weights(grid, zone))
    result = measure_observed_vegetation(observation, S2_NDVI_VEGETATION_V0, zone)
    assert result.threshold == 0.4 and [t for t, _ in result.sensitivity_m2] == [0.3, 0.4, 0.5]


def test_inconsistent_shapes_are_refused(tmp_path):
    grid = make_grid(2, 2)
    zone = zone_for(tmp_path, box(X0, Y0, X0 + 20, Y0 + 20))
    weights = prepare_pixel_area_weights(grid, zone)
    with pytest.raises(ValueError, match="grille"):
        measure_vegetation(np.zeros((3, 2), np.float32), np.ones((3, 2), bool), weights, grid, zone,
                           threshold=0.4, policy="usable_land")
    with pytest.raises(ValueError, match="SCL"):
        measure_vegetation(np.zeros((2, 2), np.float32), np.ones((2, 2), bool), weights, grid, zone,
                           threshold=0.4, policy="usable_land", scl=(np.zeros((1, 2), np.uint8), np.ones((1, 2))))


# --- Quality -------------------------------------------------------------------------------------------------


def coverage_case(tmp_path, observed_columns):
    """10 × 10 grid fully inside the zone; the first ``observed_columns`` columns are valid."""
    grid = make_grid(10, 10)
    zone = zone_for(tmp_path, box(X0, Y0, X0 + 100, Y0 + 100))
    valid = np.zeros((10, 10), bool)
    valid[:, :observed_columns] = True
    return measure(grid, zone, np.full((10, 10), 0.8, np.float32), valid)


def test_no_usable_observation_is_unavailable_without_division(tmp_path):
    result = coverage_case(tmp_path, 0)
    assert result.observed_m2 == 0 and result.vegetated_share_percent is None
    assessment = vegetation_estimate_assessment(result, S2_NDVI_VEGETATION_V0)
    assert not assessment.available and "Aucune surface observée" in assessment.reasons[0]


@pytest.mark.parametrize("coverage, available", [(29.999, False), (30.0, True)])
def test_coverage_exactly_at_the_minimum_is_published(coverage, available):
    stub = SimpleNamespace(observed_m2=coverage, observed_coverage_percent=coverage)
    assert vegetation_estimate_assessment(stub, S2_NDVI_VEGETATION_V0).available is available


# 3 columns are not exactly 30 % in equal-area terms (UTM cells differ slightly): the boundary is tested above.
@pytest.mark.parametrize("columns, available", [(2, False), (4, True), (10, True)])
def test_minimum_coverage_gates_the_territorial_estimate(tmp_path, columns, available):
    result = coverage_case(tmp_path, columns)
    assessment = vegetation_estimate_assessment(result, S2_NDVI_VEGETATION_V0)
    assert assessment.available is available and assessment.scientific_status == EXPERIMENTAL
    if available:
        assert assessment.reliability == "faible"                     # Capped, even at 100 % coverage.
        assert any("non validée" in r for r in assessment.reasons)
        assert any("représentativité" in r for r in assessment.reasons)
    else:
        assert assessment.reliability == "non évaluée" and "30 %" in assessment.reasons[0]
        assert "diagnostic" in assessment.reasons[0]


def test_reliability_never_exceeds_medium_even_for_a_validated_profile(tmp_path):
    validated = replace(S2_NDVI_VEGETATION_V0, scientific_status=VALIDATED, reliability_cap="élevée")
    assessment = vegetation_estimate_assessment(coverage_case(tmp_path, 10), validated)
    assert assessment.reliability == "moyenne" and not any("non validée" in r for r in assessment.reasons)


def test_profile_type():
    assert isinstance(S2_NDVI_VEGETATION_V0, VegetationIndexProfile)
