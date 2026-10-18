"""Synthetic integration checks for validated Sentinel-2 pair inputs."""

from dataclasses import replace
from datetime import datetime, timezone

import geopandas as gpd
import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import box

from src.data.acquisition import SeasonalContext, create_sentinel2_acquisition
from src.geo.change_analysis import ChangeClass
from src.geo.pair_selection import (
    PairSelectionConfig,
    PairSearchScope,
    PairSuitability,
    TemporalPair,
    select_temporal_pairs,
)
from src.services.temporal_analysis import (
    SeasonalComparabilityAssessment,
    analyze_temporal_vegetation_change,
)


PRODUCT_A = "S2A_SYNTHETIC_A"
PRODUCT_B = "S2B_SYNTHETIC_B"
TRANSFORM = from_origin(0, 4, 1, 1)


def _write_raster(path, values, *, product_id, nodata, transform=TRANSFORM):
    values = np.asarray(values)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=values.shape[1],
        height=values.shape[0],
        count=1,
        dtype=values.dtype,
        crs="EPSG:6933",
        transform=transform,
        nodata=nodata,
    ) as destination:
        destination.write(values, 1)
        destination.update_tags(PRODUCT_ID=product_id)
    return path


def _make_acquisition(tmp_path, product_id, acquired_at, *, red_dn, nir_dn,
                      red_offset, nir_offset, b08_transform=TRANSFORM,
                      b04_transform=TRANSFORM, embedded_product_id=None,
                      calibration=True, seasonal_context=None,
                      scl_values=None, scl_transform=None,
                      raster_shape=(4, 4)):
    red_path = _write_raster(
        tmp_path / f"{product_id}-red.tif",
        np.full(raster_shape, red_dn, dtype=np.uint16),
        product_id=product_id,
        nodata=0,
        transform=b04_transform,
    )
    nir_path = tmp_path / f"{product_id}-nir.tif"
    with rasterio.open(
        nir_path,
        "w",
        driver="GTiff",
        width=raster_shape[1],
        height=raster_shape[0],
        count=1,
        dtype="uint16",
        crs="EPSG:6933",
        transform=b08_transform if b08_transform != TRANSFORM else b04_transform,
        nodata=0,
    ) as destination:
        destination.write(np.full(raster_shape, nir_dn, dtype=np.uint16), 1)
        destination.update_tags(PRODUCT_ID=product_id)

    if scl_values is None:
        scl_values = np.full(raster_shape, 4, dtype=np.uint8)
        if product_id == PRODUCT_B:
            scl_values[1, 1] = 7
    scl_path = _write_raster(
        tmp_path / f"{product_id}-scl.tif",
        scl_values,
        product_id=product_id,
        nodata=0,
        transform=scl_transform if scl_transform is not None else b04_transform,
    )
    xml_path = tmp_path / f"{product_id}-metadata.xml"
    product_uri = embedded_product_id or f"{product_id}.SAFE"
    if calibration:
        xml_path.write_text(
            f"""<Product>
            <PRODUCT_URI>{product_uri}</PRODUCT_URI>
            <BOA_QUANTIFICATION_VALUE>10000</BOA_QUANTIFICATION_VALUE>
            <BOA_ADD_OFFSET band_id="3">{red_offset}</BOA_ADD_OFFSET>
            <BOA_ADD_OFFSET band_id="7">{nir_offset}</BOA_ADD_OFFSET>
            </Product>""",
            encoding="utf-8",
        )
    else:
        xml_path.write_text(
            f"""<Product><PRODUCT_URI>{product_uri}</PRODUCT_URI>
            <BOA_QUANTIFICATION_VALUE>10000</BOA_QUANTIFICATION_VALUE>
            <BOA_ADD_OFFSET band_id="3">{red_offset}</BOA_ADD_OFFSET>
            </Product>""",
            encoding="utf-8",
        )

    return create_sentinel2_acquisition(
        product_id=product_id,
        acquired_at=acquired_at,
        b04_path=red_path,
        b08_path=nir_path,
        scl_path=scl_path,
        calibration_xml_path=xml_path,
        asset_product_ids={
            "b04": product_id,
            "b08": product_id,
            "scl": product_id,
            "calibration_xml": product_id,
        },
        quality={"usable_land_percent": 95.0},
        seasonal_context=seasonal_context,
    )


def _selected_pair(earlier, later):
    return TemporalPair(
        earlier_product_id=earlier.product_id,
        later_product_id=later.product_id,
        earlier_datetime=earlier.acquired_at,
        later_datetime=later.acquired_at,
        interval_days=(later.acquired_at - earlier.acquired_at).total_seconds() / 86400,
        common_usable_pixels=0,
        common_area_m2=0.0,
        common_coverage_percent=0.0,
        min_usable_land_percent=0.0,
        min_aoi_coverage_percent=0.0,
        min_aoi_data_coverage_percent=0.0,
        cloud_cover_sum_percent=None,
        suitability=PairSuitability.INSUFFICIENT,
        scl_policy="usable_land",
    )


@pytest.mark.parametrize("policy,selected,expected", [
    (None, False, [False, True, True, False]),
    ("land_surface", False, [True, True, True, False]),
    ("vegetation", False, [False, True, False, False]),
    ("land_surface", True, [True, True, True, False]),
])
def test_service_default_and_explicit_scl_policies(tmp_path, policy, selected, expected):
    classes = np.array([[2, 4, 5, 7]], dtype=np.uint8)
    acquisitions = [
        _make_acquisition(tmp_path, product, datetime(2025, 1, day, tzinfo=timezone.utc),
                          red_dn=2000, nir_dn=5000, red_offset=-1000, nir_offset=-1000,
                          scl_values=classes, raster_shape=(1, 4))
        for product, day in ((PRODUCT_A, 1), (PRODUCT_B, 11))
    ]
    pair = replace(_selected_pair(*acquisitions), scl_policy=policy) if selected else None
    result = analyze_temporal_vegetation_change(
        *acquisitions, selected_pair=pair, scl_policy=None if selected else policy)
    assert result.scl_policy == (policy or "usable_land")
    assert result.comparison.comparison_mask.tolist() == [expected]


def test_two_acquisitions_calibrate_align_and_analyze_partial_aoi(tmp_path):
    first = _make_acquisition(
        tmp_path,
        PRODUCT_A,
        datetime(2025, 1, 1, tzinfo=timezone.utc),
        red_dn=3000,
        nir_dn=6000,
        red_offset=-1000,
        nir_offset=-1000,
    )
    second = _make_acquisition(
        tmp_path,
        PRODUCT_B,
        datetime(2025, 1, 11, tzinfo=timezone.utc),
        red_dn=2000,
        nir_dn=7000,
        red_offset=-500,
        nir_offset=-1500,
    )

    aoi_path = tmp_path / "partial-aoi.gpkg"
    gpd.GeoDataFrame(
        {"geometry": [box(0.5, 0.5, 3.5, 3.5)]}, crs="EPSG:6933"
    ).to_file(aoi_path, driver="GPKG")

    result = analyze_temporal_vegetation_change(
        first,
        second,
        selected_pair=_selected_pair(first, second),
        aoi_path=aoi_path,
        scl_policy="usable_land",
    )

    first_ndvi = ((6000 - 1000) - (3000 - 1000)) / (
        (6000 - 1000) + (3000 - 1000)
    )
    second_ndvi = ((7000 - 1500) - (2000 - 500)) / (
        (7000 - 1500) + (2000 - 500)
    )
    expected_delta = second_ndvi - first_ndvi
    assert first.product_id == PRODUCT_A
    assert second.product_id == PRODUCT_B
    assert first.acquired_at < second.acquired_at
    assert first.b04.product_id == first.b08.product_id == first.scl.product_id
    assert first.crs == rasterio.crs.CRS.from_epsg(6933)
    assert first.resolution == (1.0, 1.0)
    assert first.b04.nodata == first.b08.nodata == 0
    assert first.quality["usable_land_percent"] == 95.0
    assert result.earlier.product_id == PRODUCT_A
    assert result.later.product_id == PRODUCT_B
    assert result.earlier.acquired_at_utc == first.acquired_at
    assert result.earlier.asset_paths["calibration_xml"] == str(first.calibration_xml)
    assert result.earlier.asset_product_ids["b04"] == PRODUCT_A
    assert result.earlier.asset_metadata["scl"].nodata == 0
    assert result.aoi_path == str(aoi_path)
    assert result.earlier.calibration == first.calibration
    assert result.selected_pair is not None
    assert result.scl_policy == "usable_land"
    assert result.threshold == 0.1
    assert result.common_grid == result.comparison.grid
    assert "ne permet pas" in result.interpretation_scope
    assert result.comparison.comparison_mask.sum() == 15
    np.testing.assert_allclose(
        result.comparison.delta_ndvi[result.comparison.comparison_mask],
        expected_delta,
        rtol=0,
        atol=1e-7,
    )
    assert result.analysis.change_class_map[1, 1] == ChangeClass.NON_COMPARABLE
    assert np.all(
        result.analysis.change_class_map[result.comparison.comparison_mask]
        == ChangeClass.INCREASE
    )
    assert result.analysis.comparable_coverage_percent == pytest.approx(
        8 / 9 * 100, abs=1e-8
    )
    assert result.analysis.statistics["count"] == 15


def test_service_crops_processing_to_aoi_and_preserves_partial_edge_cells(tmp_path):
    first = _make_acquisition(
        tmp_path,
        PRODUCT_A,
        datetime(2025, 1, 1, tzinfo=timezone.utc),
        red_dn=3000,
        nir_dn=6000,
        red_offset=-1000,
        nir_offset=-1000,
        raster_shape=(20, 20),
        b04_transform=from_origin(0, 20, 1, 1),
    )
    second = _make_acquisition(
        tmp_path,
        PRODUCT_B,
        datetime(2025, 1, 11, tzinfo=timezone.utc),
        red_dn=2000,
        nir_dn=7000,
        red_offset=-500,
        nir_offset=-1500,
        raster_shape=(20, 20),
        b04_transform=from_origin(0, 20, 1, 1),
    )
    aoi_path = tmp_path / "small-partial-aoi.gpkg"
    gpd.GeoDataFrame(
        {"geometry": [box(5.8, 5.8, 14.2, 14.2)]}, crs="EPSG:6933"
    ).to_file(aoi_path, driver="GPKG")

    result = analyze_temporal_vegetation_change(
        first,
        second,
        aoi_path=aoi_path,
        selected_pair=_selected_pair(first, second),
        scl_policy="usable_land",
    )

    assert (result.common_grid.height, result.common_grid.width) == (10, 10)
    assert result.comparison.comparison_mask.all()
    assert result.analysis.statistics["count"] == 100
    assert result.analysis.comparable_coverage_percent == pytest.approx(100.0)
    assert result.analysis.comparable_area_m2 == pytest.approx(8.4 * 8.4)


def test_contract_rejects_asset_manifest_with_wrong_product(tmp_path):
    acquisition = _make_acquisition(
        tmp_path,
        PRODUCT_A,
        datetime(2025, 1, 1, tzinfo=timezone.utc),
        red_dn=3000,
        nir_dn=6000,
        red_offset=-1000,
        nir_offset=-1000,
    )

    with pytest.raises(ValueError, match="associés au même produit"):
        create_sentinel2_acquisition(
            product_id=PRODUCT_A,
            acquired_at=acquisition.acquired_at,
            b04_path=acquisition.b04.path,
            b08_path=acquisition.b08.path,
            scl_path=acquisition.scl.path,
            calibration_xml_path=acquisition.calibration_xml,
            asset_product_ids={
                "b04": PRODUCT_A,
                "b08": PRODUCT_B,
                "scl": PRODUCT_A,
                "calibration_xml": PRODUCT_A,
            },
        )


def test_contract_rejects_embedded_product_mismatch(tmp_path):
    with pytest.raises(ValueError, match="embarqué"):
        _make_acquisition(
            tmp_path,
            PRODUCT_A,
            datetime(2025, 1, 1, tzinfo=timezone.utc),
            red_dn=3000,
            nir_dn=6000,
            red_offset=-1000,
            nir_offset=-1000,
            embedded_product_id=f"{PRODUCT_B}.SAFE",
        )


def test_contract_rejects_missing_boa_band_offset(tmp_path):
    with pytest.raises(ValueError, match="Calibration BOA"):
        _make_acquisition(
            tmp_path,
            PRODUCT_A,
            datetime(2025, 1, 1, tzinfo=timezone.utc),
            red_dn=3000,
            nir_dn=6000,
            red_offset=-1000,
            nir_offset=-1000,
            calibration=False,
        )


def test_contract_rejects_incoherent_reflectance_grid(tmp_path):
    with pytest.raises(ValueError, match="même grille"):
        _make_acquisition(
            tmp_path,
            PRODUCT_A,
            datetime(2025, 1, 1, tzinfo=timezone.utc),
            red_dn=3000,
            nir_dn=6000,
            red_offset=-1000,
            nir_offset=-1000,
            b08_transform=from_origin(1, 4, 1, 1),
        )


def test_contract_rejects_missing_asset_path(tmp_path):
    acquisition = _make_acquisition(
        tmp_path,
        PRODUCT_A,
        datetime(2025, 1, 1, tzinfo=timezone.utc),
        red_dn=3000,
        nir_dn=6000,
        red_offset=-1000,
        nir_offset=-1000,
    )

    with pytest.raises(ValueError, match="actifs sont absents"):
        create_sentinel2_acquisition(
            product_id=PRODUCT_A,
            acquired_at=acquisition.acquired_at,
            b04_path=tmp_path / "missing.tif",
            b08_path=acquisition.b08.path,
            scl_path=acquisition.scl.path,
            calibration_xml_path=acquisition.calibration_xml,
            asset_product_ids={role: PRODUCT_A for role in (
                "b04", "b08", "scl", "calibration_xml"
            )},
        )


def test_contract_requires_timezone_aware_acquisition_date(tmp_path):
    with pytest.raises(ValueError, match="fuseau horaire"):
        _make_acquisition(
            tmp_path,
            PRODUCT_A,
            datetime(2025, 1, 1),
            red_dn=3000,
            nir_dn=6000,
            red_offset=-1000,
            nir_offset=-1000,
        )


def test_service_rejects_reversed_and_identical_acquisition_dates(tmp_path):
    first = _make_acquisition(
        tmp_path,
        PRODUCT_A,
        datetime(2025, 1, 1, tzinfo=timezone.utc),
        red_dn=3000,
        nir_dn=6000,
        red_offset=-1000,
        nir_offset=-1000,
    )
    later = _make_acquisition(
        tmp_path,
        PRODUCT_B,
        datetime(2025, 1, 11, tzinfo=timezone.utc),
        red_dn=2000,
        nir_dn=7000,
        red_offset=-500,
        nir_offset=-1500,
    )

    with pytest.raises(ValueError, match="ordre chronologique"):
        analyze_temporal_vegetation_change(later, first)

    same_date = _make_acquisition(
        tmp_path,
        "S2C_SYNTHETIC_C",
        first.acquired_at,
        red_dn=2500,
        nir_dn=6500,
        red_offset=-800,
        nir_offset=-1200,
    )
    with pytest.raises(ValueError, match="date identique"):
        analyze_temporal_vegetation_change(first, same_date)


def test_service_rejects_selected_pair_that_does_not_match_assets(tmp_path):
    first = _make_acquisition(
        tmp_path,
        PRODUCT_A,
        datetime(2025, 1, 1, tzinfo=timezone.utc),
        red_dn=3000,
        nir_dn=6000,
        red_offset=-1000,
        nir_offset=-1000,
    )
    later = _make_acquisition(
        tmp_path,
        PRODUCT_B,
        datetime(2025, 1, 11, tzinfo=timezone.utc),
        red_dn=2000,
        nir_dn=7000,
        red_offset=-500,
        nir_offset=-1500,
    )

    mismatched_pair = replace(
        _selected_pair(first, later),
        earlier_product_id="UNRELATED_PRODUCT",
    )
    with pytest.raises(ValueError, match="ne correspond pas"):
        analyze_temporal_vegetation_change(
            first,
            later,
            selected_pair=mismatched_pair,
        )


def test_service_rejects_explicit_scl_policy_mismatch_with_selected_pair(tmp_path):
    first = _make_acquisition(
        tmp_path,
        PRODUCT_A,
        datetime(2025, 1, 1, tzinfo=timezone.utc),
        red_dn=3000,
        nir_dn=6000,
        red_offset=-1000,
        nir_offset=-1000,
    )
    later = _make_acquisition(
        tmp_path,
        PRODUCT_B,
        datetime(2025, 1, 11, tzinfo=timezone.utc),
        red_dn=2000,
        nir_dn=7000,
        red_offset=-500,
        nir_offset=-1500,
    )

    with pytest.raises(ValueError, match="ne correspond pas à celle de la paire"):
        analyze_temporal_vegetation_change(
            first,
            later,
            selected_pair=_selected_pair(first, later),
            scl_policy="land_surface",
        )


def test_selected_pair_policy_and_scl_grid_are_traced_separately_from_b04_analysis(
    tmp_path,
):
    scl_grid_transform = from_origin(1, 4, 2, 2)
    earlier = _make_acquisition(
        tmp_path,
        PRODUCT_A,
        datetime(2025, 1, 1, tzinfo=timezone.utc),
        red_dn=3000,
        nir_dn=6000,
        red_offset=-1000,
        nir_offset=-1000,
        scl_values=np.array([[2, 4], [5, 0]], dtype=np.uint8),
        scl_transform=scl_grid_transform,
    )
    later = _make_acquisition(
        tmp_path,
        PRODUCT_B,
        datetime(2025, 1, 11, tzinfo=timezone.utc),
        red_dn=2000,
        nir_dn=7000,
        red_offset=-500,
        nir_offset=-1500,
        scl_values=np.array([[2, 5], [4, 4]], dtype=np.uint8),
        scl_transform=scl_grid_transform,
    )
    aoi_path = tmp_path / "partial-grid-aoi.gpkg"
    gpd.GeoDataFrame(
        {"geometry": [box(0.5, 0.5, 3.5, 3.5)]}, crs="EPSG:6933"
    ).to_file(aoi_path, driver="GPKG")
    search = [
        {"id": acquisition.product_id, "properties": {"datetime": acquisition.acquired_at.isoformat()}}
        for acquisition in (earlier, later)
    ]
    quality = {
        acquisition.product_id: {
            "usable_land_percent": 90.0,
            "aoi_coverage_percent": 100.0,
            "aoi_data_coverage_percent": 100.0,
        }
        for acquisition in (earlier, later)
    }
    selection = select_temporal_pairs(
        search,
        scl_paths={
            earlier.product_id: earlier.scl.path,
            later.product_id: later.scl.path,
        },
        quality_by_product=quality,
        aoi_path=aoi_path,
        search_scope=PairSearchScope.LOCAL_SUBSET,
        config=PairSelectionConfig(scl_policy="land_surface"),
    )
    pair = selection.pairs[0]

    result = analyze_temporal_vegetation_change(
        earlier,
        later,
        aoi_path=aoi_path,
        selected_pair=pair,
    )

    assert pair.scl_policy == result.scl_policy == "land_surface"
    assert pair.scl_coverage_grid is not None
    assert pair.scl_coverage_grid.resolution == (2.0, 2.0)
    assert pair.scl_coverage_grid.width == 2
    assert result.common_grid.resolution == (1.0, 1.0)
    assert result.common_grid.width == 4
    assert pair.common_coverage_percent == pytest.approx(75.0)
    # The selector measures its SCL grid; analysis reports its B04-grid basis.
    assert 0.0 <= result.analysis.comparable_coverage_percent <= 100.0
    assert result.analysis.comparable_mask.shape == (4, 4)
    assert result.analysis.comparable_mask.sum() < 16  # SCL NoData propagates.
    assert result.analysis.comparable_mask[0, 1]  # land_surface keeps class 2.
    assert not result.analysis.comparable_mask[2, 3]  # Source SCL NoData stays invalid.


def test_service_rejects_nonintersecting_acquisition_grids(tmp_path):
    first = _make_acquisition(
        tmp_path,
        PRODUCT_A,
        datetime(2025, 1, 1, tzinfo=timezone.utc),
        red_dn=3000,
        nir_dn=6000,
        red_offset=-1000,
        nir_offset=-1000,
    )
    later = _make_acquisition(
        tmp_path,
        PRODUCT_B,
        datetime(2025, 1, 11, tzinfo=timezone.utc),
        red_dn=2000,
        nir_dn=7000,
        red_offset=-500,
        nir_offset=-1500,
        b04_transform=from_origin(100, 104, 1, 1),
    )

    with pytest.raises(ValueError, match="aucune intersection"):
        analyze_temporal_vegetation_change(first, later)


def test_inseasonal_labels_are_preserved_and_warn_without_rejecting(tmp_path):
    earlier_context = SeasonalContext(
        region="Gabon/Estuaire",
        vegetation_type="forêt dense humide",
        season_id="period-A",
        source="validated-season-calendar",
        version="v1",
    )
    later_context = SeasonalContext(
        region="Gabon/Estuaire",
        vegetation_type="forêt dense humide",
        season_id="period-B",
        source="validated-season-calendar",
        version="v1",
    )
    exact_earlier_date = datetime(
        2025, 1, 1, 9, 23, 17, 123456, tzinfo=timezone.utc
    )
    first = _make_acquisition(
        tmp_path,
        PRODUCT_A,
        exact_earlier_date,
        red_dn=3000,
        nir_dn=6000,
        red_offset=-1000,
        nir_offset=-1000,
        seasonal_context=earlier_context,
    )
    later = _make_acquisition(
        tmp_path,
        PRODUCT_B,
        datetime(2025, 1, 11, tzinfo=timezone.utc),
        red_dn=2000,
        nir_dn=7000,
        red_offset=-500,
        nir_offset=-1500,
        seasonal_context=later_context,
    )

    result = analyze_temporal_vegetation_change(first, later)

    assert result.earlier.acquired_at_source == exact_earlier_date
    assert result.earlier.acquired_at_utc == exact_earlier_date
    assert result.earlier.seasonal_context == earlier_context
    assert result.later.seasonal_context == later_context
    assert result.seasonal_assessment.interseasonal is True
    assert result.seasonal_assessment.status == "not_assessed"
    assert any("intersaisonnière" in warning for warning in result.seasonal_assessment.warnings)
    # Interseasonal comparisons remain analyzable; the assessment is a warning.
    assert result.analysis.statistics["count"] > 0
    assert result.scl_policy == "usable_land"  # Quantitative land default without a selected pair.


def test_same_season_label_is_not_reported_as_proof_of_comparability(tmp_path):
    context = SeasonalContext(
        region="Gabon/Estuaire",
        vegetation_type="forêt dense humide",
        season_id="period-A",
        source="validated-season-calendar",
        version="v1",
    )
    first = _make_acquisition(
        tmp_path,
        PRODUCT_A,
        datetime(2025, 1, 1, tzinfo=timezone.utc),
        red_dn=3000,
        nir_dn=6000,
        red_offset=-1000,
        nir_offset=-1000,
        seasonal_context=context,
    )
    later = _make_acquisition(
        tmp_path,
        PRODUCT_B,
        datetime(2025, 1, 11, tzinfo=timezone.utc),
        red_dn=2000,
        nir_dn=7000,
        red_offset=-500,
        nir_offset=-1500,
        seasonal_context=context,
    )

    result = analyze_temporal_vegetation_change(first, later)

    assert result.seasonal_assessment.interseasonal is False
    assert result.seasonal_assessment.status == "not_assessed"
    assert any("ne garantit pas" in warning for warning in result.seasonal_assessment.warnings)


def test_seasonal_evaluator_receives_region_and_vegetation_and_never_rejects(tmp_path):
    context_a = SeasonalContext(
        region="Gabon/Estuaire",
        vegetation_type="mangrove",
        season_id="period-A",
        source="regional-phenology-study",
        version="2025.1",
    )
    context_b = SeasonalContext(
        region="Gabon/Estuaire",
        vegetation_type="mangrove",
        season_id="period-B",
        source="regional-phenology-study",
        version="2025.1",
    )
    first = _make_acquisition(
        tmp_path,
        PRODUCT_A,
        datetime(2025, 1, 1, tzinfo=timezone.utc),
        red_dn=3000,
        nir_dn=6000,
        red_offset=-1000,
        nir_offset=-1000,
        seasonal_context=context_a,
    )
    later = _make_acquisition(
        tmp_path,
        PRODUCT_B,
        datetime(2025, 1, 11, tzinfo=timezone.utc),
        red_dn=2000,
        nir_dn=7000,
        red_offset=-500,
        nir_offset=-1500,
        seasonal_context=context_b,
    )

    class RegionalEvaluator:
        def __init__(self):
            self.received = None

        def assess(self, earlier, later):
            self.received = (earlier, later)
            return SeasonalComparabilityAssessment(
                status="not_comparable",
                method="regional-phenology-study/2025.1",
                rationale="Validation régionale à confirmer par l'opérateur.",
                interseasonal=True,
                warnings=("Interprétation prudente recommandée.",),
            )

    evaluator = RegionalEvaluator()
    result = analyze_temporal_vegetation_change(
        first,
        later,
        seasonal_evaluator=evaluator,
    )

    assert evaluator.received == (context_a, context_b)
    assert result.seasonal_assessment.status == "not_comparable"
    assert result.seasonal_assessment.method == "regional-phenology-study/2025.1"
    assert result.seasonal_assessment.interseasonal is True
    assert any("intersaisonnière" in warning for warning in result.seasonal_assessment.warnings)
    assert result.analysis.statistics["count"] > 0
