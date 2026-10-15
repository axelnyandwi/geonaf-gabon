"""Deterministic serialization tests for temporal change GeoTIFF exports."""

import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType

import geopandas as gpd
import numpy as np
import pytest
import rasterio
from affine import Affine
from rasterio.crs import CRS
from shapely.geometry import box

from src.data.acquisition import BOACalibration, SeasonalContext
from src.data.acquisition import RasterAsset
from src.geo.change_analysis import ChangeClass, analyze_ndvi_changes
from src.geo.temporal import RasterGrid, TemporalComparison
from src.services.geotiff_export import export_temporal_change_geotiff
from src.services.temporal_analysis import (
    AcquisitionProvenance,
    SeasonalComparabilityAssessment,
    TemporalVegetationChangeResult,
)


def _context(season_id):
    return SeasonalContext(
        region="region-a",
        vegetation_type="forest-type-a",
        season_id=season_id,
        source="validated-season-catalogue",
        version="2025.1",
    )


def _provenance(product_id, acquired_at, context):
    transform = Affine(100, 0, 0, 0, -100, 300)
    asset = RasterAsset(
        path=Path(f"{product_id}-B04.tif"),
        product_id=product_id,
        crs=CRS.from_epsg(6933),
        resolution=(100.0, 100.0),
        nodata=0,
        width=3,
        height=3,
        transform=transform,
    )
    return AcquisitionProvenance(
        product_id=product_id,
        acquired_at_source=acquired_at,
        acquired_at_utc=acquired_at,
        asset_paths=MappingProxyType({"b04": str(asset.path)}),
        asset_metadata=MappingProxyType({"b04": asset}),
        asset_product_ids=MappingProxyType({"b04": product_id}),
        calibration=BOACalibration(10000.0, -1000.0, -1000.0),
        quality=MappingProxyType({"usable_land_percent": 95.0}),
        seasonal_context=context,
    )


def _result(aoi_path=None, *, earlier_context=None, later_context=None, relation="unknown"):
    grid = RasterGrid(
        crs=CRS.from_epsg(6933),
        resolution=(100.0, 100.0),
        transform=Affine(100, 0, 0, 0, -100, 300),
        width=3,
        height=3,
    )
    classes = np.full((3, 3), ChangeClass.DECREASE, dtype=np.uint8)
    delta = np.full((3, 3), -0.2, dtype=np.float32)
    comparable = np.ones((3, 3), dtype=bool)
    comparison = TemporalComparison(
        grid=grid,
        ndvi_date_1=np.full((3, 3), 0.5, dtype=np.float32),
        ndvi_date_2=np.full((3, 3), 0.3, dtype=np.float32),
        valid_date_1=comparable,
        valid_date_2=comparable,
        comparison_mask=comparable,
        delta_ndvi=delta,
    )
    analysis = analyze_ndvi_changes(comparison, threshold=0.1, aoi_path=aoi_path)
    season_warnings = {
        "unknown": "Comparabilité saisonnière non évaluée : contexte incomplet.",
        "same": "Même saison déclarée : cela ne garantit pas la comparabilité.",
        "different": "Comparaison intersaisonnière : variation saisonnière possible.",
    }
    assessment = SeasonalComparabilityAssessment(
        status="not_assessed",
        method="metadata_only",
        rationale="Synthetic deterministic assessment.",
        interseasonal={"unknown": None, "same": False, "different": True}[relation],
        warnings=(season_warnings[relation],),
        season_relation=relation,
    )
    timestamp = datetime(2025, 1, 1, tzinfo=timezone.utc)
    return TemporalVegetationChangeResult(
        earlier=_provenance("PRODUCT_A", timestamp, earlier_context),
        later=_provenance("PRODUCT_B", datetime(2025, 7, 1, tzinfo=timezone.utc), later_context),
        selected_pair=None,
        seasonal_assessment=assessment,
        scl_policy="land_surface",
        threshold=0.1,
        common_grid=grid,
        comparison=comparison,
        analysis=analysis,
        aoi_path=str(aoi_path) if aoi_path else None,
    )


@pytest.mark.parametrize(
    ("relation", "earlier_context", "later_context"),
    [
        ("unknown", None, _context("dry-2025")),
        ("different", _context("wet-2024"), _context("dry-2025")),
        ("same", _context("wet-2025"), _context("wet-2025")),
    ],
)
def test_manifest_preserves_season_relation_contexts_and_warnings(
    tmp_path, relation, earlier_context, later_context
):
    result = _result(
        earlier_context=earlier_context,
        later_context=later_context,
        relation=relation,
    )
    outputs = export_temporal_change_geotiff(result, tmp_path / "change.tif")

    manifest = json.loads(outputs["manifest"].read_text(encoding="utf-8"))
    assert manifest["seasonality"]["relation"] == relation
    assert manifest["seasonality"]["assessment"]["warnings"]
    if relation == "unknown":
        assert manifest["seasonality"]["earlier_context"] is None
    else:
        assert manifest["seasonality"]["earlier_context"]["source"] == "validated-season-catalogue"
        assert manifest["seasonality"]["earlier_context"]["version"] == "2025.1"
        assert manifest["seasonality"]["earlier_context"]["season_id"] == earlier_context.season_id
        assert manifest["seasonality"]["later_context"]["season_id"] == later_context.season_id
        assert manifest["seasonality"]["later_context"]["source"] == later_context.source
        assert manifest["seasonality"]["later_context"]["version"] == later_context.version
    if relation == "same":
        assert "ne garantit pas" in manifest["seasonality"]["assessment"]["warnings"][0]
    with rasterio.open(outputs["change_class"]) as exported:
        assert exported.tags()["SEASON_RELATION"] == relation
        assert exported.tags()["MANIFEST"] == outputs["manifest"].name


def test_export_aoi_edge_pixels_and_nodata_preserve_weighted_surface_indicators(tmp_path):
    aoi_path = tmp_path / "partial-aoi.gpkg"
    gpd.GeoDataFrame(
        {"geometry": [box(25, 125, 175, 275)]}, crs="EPSG:6933"
    ).to_file(aoi_path, driver="GPKG")
    result = _result(aoi_path)
    outputs = export_temporal_change_geotiff(result, tmp_path / "edge.tif")

    with rasterio.open(outputs["change_class"]) as class_raster, rasterio.open(
        outputs["delta_ndvi"]
    ) as delta_raster:
        class_mask = class_raster.dataset_mask() > 0
        delta_mask = delta_raster.dataset_mask() > 0
        assert np.array_equal(class_mask, delta_mask)
        assert int(class_mask.sum()) == 4  # all four cells intersect the partial AOI
        assert np.all(class_raster.read(1)[class_mask] == ChangeClass.DECREASE)
        assert np.all(class_raster.read(1)[~class_mask] == 0)
        assert np.all(np.isnan(delta_raster.read(1)[~delta_mask]))
        assert class_raster.nodata == 0
        assert np.isnan(delta_raster.nodata)

    manifest = json.loads(outputs["manifest"].read_text(encoding="utf-8"))
    raster = manifest["raster_representation"]
    assert raster["valid_pixel_count"] == 4
    assert raster["exported_pixel_counts"]["decrease"] == 4
    assert raster["nodata"] == {"change_class": 0, "delta_ndvi": "NaN"}
    assert manifest["analysis_indicators"]["areas_m2"]["decrease"] == 22500.0
    assert "intersection exacte" in raster["surface_note"]


def test_export_refuses_to_overwrite_existing_outputs(tmp_path):
    result = _result()
    class_path = tmp_path / "existing.tif"
    class_path.write_bytes(b"keep")

    with pytest.raises(FileExistsError):
        export_temporal_change_geotiff(result, class_path)

    assert class_path.read_bytes() == b"keep"


def test_long_tag_values_are_bounded_and_preserved_in_json(tmp_path):
    result = _result()
    long_product_id = "PRODUCT_" + "X" * 300
    result = replace(result, earlier=replace(result.earlier, product_id=long_product_id))

    outputs = export_temporal_change_geotiff(result, tmp_path / "long.tif")
    manifest = json.loads(outputs["manifest"].read_text(encoding="utf-8"))
    with rasterio.open(outputs["change_class"]) as exported:
        bounded_tag = exported.tags()["PRODUCT_EARLIER"]
    assert len(bounded_tag) == 256
    assert bounded_tag.startswith(long_product_id[:253])
    assert manifest["inputs"]["earlier"]["product_id"] == long_product_id
