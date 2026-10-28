"""Spectral class transitions on the strictly common surface of two acquisitions; synthetic and chain checks."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import numpy as np
import pytest
from affine import Affine
from rasterio.crs import CRS
from shapely.geometry import box

from src.geo.change_analysis import load_equal_area_aoi, prepare_pixel_area_weights
from src.geo.scientific_profile import GABON_EQUATORIAL_EXPERIMENTAL as AKANDA
from src.indicators.profiles import (
    AFRICA_DEFAULT_V1, S2_NDVI_TRANSITION_V0, S2_NDVI_VEGETATION_V0, VALIDATED, TransitionProfile,
)
from src.indicators.quality import pair_assessment, same_lattice_pair
from src.indicators.transitions_s2 import (
    block_diagnostic, interval_days, measure_pair, measure_pair_result, weighted_median,
)
from tests.test_indicators_modules import AFRICA_DEFAULT_V1_FINGERPRINT
from tests.test_local_preview import local_data  # noqa: F401  (fixture)
from tests.test_vegetation_s2 import SCIENTIFIC_FINGERPRINTS, X0, Y0, layout, make_grid, zone_for

VEGETATION_FINGERPRINT = "b218a84cede9f2d9d8a7c5700ea9a037b49b9daf65da5e9d205d6abeb0f14000"      # L3a, unchanged.
TRANSITION_FINGERPRINT = "0bc29958f8c5e08c63a491dba21473da440368e4da0a8d7cd379ceef84415e9a"      # L4a.
T = np.float32(0.4)
BELOW = np.nextafter(T, np.float32(-1))


def run(grid, zone, ndvi_1, ndvi_2, comparable=None, valid_1=None, valid_2=None, **kwargs):
    ones = np.ones(ndvi_1.shape, bool)
    comparable = ones if comparable is None else comparable
    kwargs.setdefault("threshold", 0.4)
    return measure_pair(ndvi_1.astype(np.float32), ndvi_2.astype(np.float32), comparable,
                        ones if valid_1 is None else valid_1, ones if valid_2 is None else valid_2,
                        prepare_pixel_area_weights(grid, zone), grid, zone, **kwargs)


def square(tmp_path, size=10, name="zone"):
    grid = make_grid(size, size)
    return grid, zone_for(tmp_path, box(X0, Y0, X0 + 10 * size, Y0 + 10 * size), name=name)


# --- Profile --------------------------------------------------------------------------------------------------


def test_transition_profile_is_versioned_and_nests_the_class_threshold():
    profile = S2_NDVI_TRANSITION_V0
    assert profile.threshold == 0.4 and profile.vegetation is S2_NDVI_VEGETATION_V0
    assert (profile.min_common_coverage_percent, profile.short_interval_days) == (30.0, 30.0)
    assert (profile.block_size_m, profile.block_crs, profile.reliability_cap) == (1000.0, "EPSG:6933", "faible")
    assert profile.fingerprint == TRANSITION_FINGERPRINT
    changed = replace(profile, vegetation=replace(S2_NDVI_VEGETATION_V0, threshold=0.5,
                                                  sensitivity_thresholds=(0.3, 0.4, 0.5)))
    assert changed.fingerprint != profile.fingerprint                      # The class threshold is inside.
    assert replace(profile, min_common_coverage_percent=40.0).fingerprint != profile.fingerprint
    # Existing fingerprints untouched; the ΔNDVI variation threshold stays 0.10 in the scientific profile.
    assert S2_NDVI_VEGETATION_V0.fingerprint == VEGETATION_FINGERPRINT
    assert AFRICA_DEFAULT_V1.fingerprint == AFRICA_DEFAULT_V1_FINGERPRINT
    assert AKANDA.fingerprint == SCIENTIFIC_FINGERPRINTS["akanda"] and AKANDA.change_threshold == 0.10


@pytest.mark.parametrize("changes", [{"min_common_coverage_percent": 101.0}, {"short_interval_days": 0},
                                     {"block_size_m": -1.0}, {"block_min_zone_share": 1.5},
                                     {"scientific_status": "proven"}, {"reliability_cap": "totale"},
                                     {"vegetation": None}, {"block_crs": " "}], ids=lambda c: next(iter(c)))
def test_invalid_transition_profiles_are_refused(changes):
    with pytest.raises(ValueError):
        replace(S2_NDVI_TRANSITION_V0, **changes)


# --- Weighted statistics ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("values, weights, expected", [
    ([1, 2, 3], [1, 1, 1], 2), ([1, 2], [1, 1], 1), ([1, 2], [1, 3], 2), ([3, 1, 2], [0, 1, 0], 1),
    ([5], [2], 5), ([], [], None), ([1, 2], [0, 0], None)])
def test_lower_weighted_median(values, weights, expected):
    assert weighted_median(values, weights) == expected


def test_partial_pixels_weight_the_statistics(tmp_path):
    grid, zone, ndvi = layout(tmp_path, 32632, X0, Y0)      # Corners: quarter pixels at 0.9, else 0.1.
    result = run(grid, zone, ndvi, np.full((4, 4), 0.1, np.float32))
    weights = prepare_pixel_area_weights(grid, zone)
    corners = weights[[0, 0, 3, 3], [0, 3, 0, 3]].sum()
    assert result.common_m2 == pytest.approx(900.0, rel=2e-3) and result.common_pixels == 16
    assert result.mean_1 == pytest.approx((0.9 * corners + 0.1 * (weights.sum() - corners)) / weights.sum(),
                                          rel=1e-6)
    assert result.mean_1 == pytest.approx((0.9 * 100 + 0.1 * 800) / 900, rel=2e-3)   # Not the pixel mean 0.3.
    assert result.median_1 == pytest.approx(0.1) and result.delta_mean == pytest.approx(0.1 - result.mean_1)
    assert result.transitions.down_m2 == pytest.approx(corners, rel=1e-12)


def test_delta_statistics_are_consistent(tmp_path):
    grid, zone = square(tmp_path, 20)
    rng = np.random.default_rng(4)
    n1, n2 = (rng.uniform(-0.2, 0.9, (20, 20)).astype(np.float32) for _ in range(2))
    result = run(grid, zone, n1, n2)
    weights = prepare_pixel_area_weights(grid, zone)
    assert result.delta_mean == result.mean_2 - result.mean_1
    assert result.delta_mean == pytest.approx(np.average(n2.astype(np.float64) - n1, weights=weights), abs=1e-12)
    assert result.delta_median == weighted_median((n2.astype(np.float64) - n1).ravel(), weights.ravel())


# --- Transitions --------------------------------------------------------------------------------------------------


def test_threshold_equality_in_each_transition(tmp_path):
    grid, zone = square(tmp_path, 2)
    ndvi_1 = np.array([[T, T], [BELOW, BELOW]], np.float32)
    ndvi_2 = np.array([[BELOW, T], [T, BELOW]], np.float32)
    result = run(grid, zone, ndvi_1, ndvi_2)
    cell = result.common_m2 / 4
    t = result.transitions
    for value in (t.down_m2, t.stayed_above_m2, t.up_m2, t.stayed_below_m2):
        assert value == pytest.approx(cell, rel=1e-3)
    assert t.net_m2 == pytest.approx(t.up_m2 - t.down_m2)


def test_transitions_add_up_to_the_common_surface_for_every_threshold(tmp_path):
    grid, zone = square(tmp_path, 30)
    rng = np.random.default_rng(8)
    n1, n2 = (rng.uniform(-0.2, 0.9, (30, 30)).astype(np.float32) for _ in range(2))
    comparable = rng.random((30, 30)) > 0.3
    result = run(grid, zone, n1, n2, comparable, sensitivity=(0.3, 0.5))
    assert [s.threshold for s in result.sensitivity] == [0.3, 0.4, 0.5]
    assert result.sensitivity[1] is result.transitions
    for s in result.sensitivity:
        total = s.down_m2 + s.up_m2 + s.stayed_above_m2 + s.stayed_below_m2
        assert total == pytest.approx(result.common_m2, rel=1e-12)


def test_only_the_intersection_of_both_masks_is_compared(tmp_path):
    grid, zone = square(tmp_path, 10)
    valid_1, valid_2 = np.zeros((10, 10), bool), np.zeros((10, 10), bool)
    valid_1[:, :6], valid_2[:, 4:] = True, True                          # 60 % each, 20 % in common.
    ndvi = np.full((10, 10), 0.8, np.float32)
    result = run(grid, zone, ndvi, ndvi, valid_1 & valid_2, valid_1, valid_2)
    assert result.valid_1_m2 == pytest.approx(0.6 * result.zone_m2, rel=1e-3)
    assert result.common_coverage_percent == pytest.approx(20.0, rel=1e-3)      # Never the union (100 %).
    assessment = pair_assessment(result, S2_NDVI_TRANSITION_V0, interval_days=60.0, aligned=True)
    assert not assessment.available and "Couverture commune 20,0 % < 30 %" in assessment.reasons[0]


def test_no_common_surface_gives_no_statistic(tmp_path):
    grid, zone = square(tmp_path, 4)
    result = run(grid, zone, np.zeros((4, 4)), np.zeros((4, 4)), np.zeros((4, 4), bool))
    assert result.common_m2 == 0 and result.mean_1 is None and result.delta_median is None
    assert result.transitions.down_m2 == result.transitions.up_m2 == 0
    assessment = pair_assessment(result, S2_NDVI_TRANSITION_V0, interval_days=60.0, aligned=True)
    assert not assessment.available and "Aucune surface observée commune" in assessment.reasons[0]


def test_float32_and_shapes_are_required(tmp_path):
    grid, zone = square(tmp_path, 2)
    weights, ones = prepare_pixel_area_weights(grid, zone), np.ones((2, 2), bool)
    with pytest.raises(TypeError, match="float32"):
        measure_pair(np.zeros((2, 2)), np.zeros((2, 2), np.float32), ones, ones, ones, weights, grid, zone,
                     threshold=0.4)
    with pytest.raises(ValueError, match="grille"):
        measure_pair(np.zeros((2, 3), np.float32), np.zeros((2, 3), np.float32), ones, ones, ones, weights, grid,
                     zone, threshold=0.4)


# --- Quality ------------------------------------------------------------------------------------------------------


def lattice_asset(x0=X0, y0=Y0 + 100, res=10.0, epsg=32632):
    return SimpleNamespace(transform=Affine(res, 0, x0, 0, -res, y0), crs=CRS.from_epsg(epsg))


@pytest.mark.parametrize("asset, aligned", [
    (lattice_asset(), True), (lattice_asset(x0=X0 - 30), True), (lattice_asset(x0=X0 + 5), False),
    (lattice_asset(res=20.0), False), (lattice_asset(epsg=32633), False)],
    ids=["identique", "décalage de 3 pixels", "décalage de 5 m", "20 m", "autre CRS"])
def test_grids_must_share_the_lattice(asset, aligned):
    grid = make_grid(10, 10)
    assert same_lattice_pair(lattice_asset(), asset, grid) is aligned


def test_quality_gates_warnings_and_reliability(tmp_path):
    grid, zone = square(tmp_path, 10)
    full = run(grid, zone, np.full((10, 10), 0.8), np.full((10, 10), 0.2))
    ok = pair_assessment(full, S2_NDVI_TRANSITION_V0, interval_days=10.0, aligned=True)
    assert ok.available and ok.reliability == "faible" and ok.scientific_status == "experimental"
    assert any("Intervalle court (10,0 jours < 30)" in w for w in ok.warnings)        # Warning, not a gate.
    assert any("sans attribution de cause" in r for r in ok.reasons)
    assert not any("Intervalle court" in w for w in pair_assessment(
        full, S2_NDVI_TRANSITION_V0, interval_days=45.0, aligned=True).warnings)
    misaligned = pair_assessment(full, S2_NDVI_TRANSITION_V0, interval_days=45.0, aligned=False)
    assert not misaligned.available and "aucun rééchantillonnage" in misaligned.reasons[0]
    for days in (0.0, -3.0):
        assert not pair_assessment(full, S2_NDVI_TRANSITION_V0, interval_days=days, aligned=True).available
    validated = replace(S2_NDVI_TRANSITION_V0, scientific_status=VALIDATED, reliability_cap="élevée")
    assert pair_assessment(full, validated, interval_days=45.0, aligned=True).reliability == "moyenne"
    assert interval_days(datetime(2025, 1, 15, tzinfo=timezone.utc),
                         datetime(2025, 1, 15, tzinfo=timezone.utc) + timedelta(days=10)) == 10.0


# --- Spatial diagnostic by fixed 1 km² blocks ----------------------------------------------------------------------


def block_case(tmp_path, offset_pixels=0, name="z"):
    """3 km × 3 km zone; the image grid origin moves with ``offset_pixels`` while the zone stays put."""
    x0 = X0 - 10 * offset_pixels
    grid = make_grid(300 + offset_pixels, 300, x0=x0)
    zone = zone_for(tmp_path, box(X0, Y0, X0 + 3000, Y0 + 3000), name=name)
    return grid, zone, prepare_pixel_area_weights(grid, zone)


def test_blocks_are_fixed_in_the_equal_area_reference(tmp_path):
    grid, zone, weights = block_case(tmp_path)
    full = block_diagnostic(np.ones(weights.shape, bool), weights, grid, size_m=1000.0, crs="EPSG:6933",
                            min_zone_share=0.5, common_min_share=0.1)
    assert full.eligible_blocks >= 4 and full.covered_share == 1.0 and full.empty_share == 0.0
    none = block_diagnostic(np.zeros(weights.shape, bool), weights, grid, size_m=1000.0, crs="EPSG:6933",
                            min_zone_share=0.5, common_min_share=0.1)
    assert none.eligible_blocks == full.eligible_blocks and none.covered_share == 0.0 and none.empty_share == 1.0
    # Same zone, image grid shifted by 7 pixels: the blocks do not depend on the image grid.
    grid_2, _, weights_2 = block_case(tmp_path, offset_pixels=7, name="z2")
    west = np.zeros(weights.shape, bool)
    west[:, :150] = True
    west_2 = np.zeros(weights_2.shape, bool)
    west_2[:, :157] = True
    first = block_diagnostic(west, weights, grid, size_m=1000.0, crs="EPSG:6933", min_zone_share=0.5,
                             common_min_share=0.1)
    second = block_diagnostic(west_2, weights_2, grid_2, size_m=1000.0, crs="EPSG:6933", min_zone_share=0.5,
                              common_min_share=0.1)
    assert (first.eligible_blocks, first.covered_share, first.empty_share) == \
        (second.eligible_blocks, second.covered_share, second.empty_share)
    assert 0 < first.covered_share < 1


def test_zone_smaller_than_a_block_has_no_spatial_diagnostic(tmp_path):
    grid, zone = square(tmp_path, 20)                                   # 200 m × 200 m.
    result = run(grid, zone, np.full((20, 20), 0.8), np.full((20, 20), 0.8))
    assert result.blocks.eligible_blocks == 0 and result.blocks.covered_share is None
    assessment = pair_assessment(result, S2_NDVI_TRANSITION_V0, interval_days=45.0, aligned=True)
    assert assessment.available and any("non calculable" in w for w in assessment.warnings)


# --- Non-regression against the existing pair chain ---------------------------------------------------------------


def test_common_surface_is_the_chain_one_bit_for_bit(local_data):
    from scripts.benchmark_streamlit import scientific_digest
    from src.services.local_preview import analyze_local_akanda, list_local_observations

    earlier, later = list_local_observations(local_data)
    result = analyze_local_akanda(earlier, later, local_data, scientific_profile=AKANDA)
    before = scientific_digest(result)
    zone = load_equal_area_aoi(local_data / "aoi" / "akanda.geojson")
    measure = measure_pair_result(result, S2_NDVI_TRANSITION_V0, zone)
    assert measure.common_m2 == result.analysis.comparable_area_m2
    assert measure.common_coverage_percent == pytest.approx(result.analysis.comparable_coverage_percent, rel=1e-12)
    assert scientific_digest(result) == before                         # The historical result is not modified.
    assert same_lattice_pair(result.earlier.asset_metadata["b04"], result.later.asset_metadata["b04"],
                             result.common_grid)


def test_pack_v3_pair_keeps_its_historical_digest_and_common_surface():
    import os

    from scripts.benchmark_streamlit import scientific_digest
    from src.services.local_preview import analyze_local_akanda, list_local_observations
    from tests.test_demo_pack_science import PACK, PAIR_DIGEST

    if not (PACK / "PACK_MANIFEST.json").is_file():
        pytest.skip("Paquet de démonstration absent.")
    found = {o.acquired_at.day: o for o in list_local_observations(PACK)}
    result = analyze_local_akanda(found[15], found[25], PACK, scientific_profile=AKANDA)
    measure = measure_pair_result(result, S2_NDVI_TRANSITION_V0, load_equal_area_aoi(PACK / "aoi" / "akanda.geojson"))
    assert measure.common_m2 == result.analysis.comparable_area_m2                         # Bit for bit.
    if os.name == "nt":                                                                     # Reference platform.
        assert scientific_digest(result) == PAIR_DIGEST
    assert same_lattice_pair(result.earlier.asset_metadata["b04"], result.later.asset_metadata["b04"],
                             result.common_grid)
    t = measure.transitions
    assert t.down_m2 + t.up_m2 + t.stayed_above_m2 + t.stayed_below_m2 == pytest.approx(measure.common_m2, rel=1e-12)
    assert measure.blocks.eligible_blocks > 100 and 0 < measure.blocks.covered_share < 1


def test_profile_type():
    assert isinstance(S2_NDVI_TRANSITION_V0, TransitionProfile)
