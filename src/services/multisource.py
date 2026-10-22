"""Spatial + temporal joins between Sentinel-2 results and external layers.

Engine only (no Streamlit). Inputs are arrays already on the scientific grid
(e.g. a pair result's NDVI or ΔNDVI with its validity mask); the external
layer is aligned onto that grid, never the reverse, so Sentinel-2 values are
not resampled. Every join carries the layer descriptor, licence attribution
and the temporal offset between observation and layer validity.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np

from src.data.external_sources import ExternalLayerDescriptor, TemporalJoinAssessment, assess_temporal_join
from src.geo.change_analysis import _rasterize_aoi_pixel_mask, load_equal_area_aoi, prepare_pixel_area_weights
from src.geo.temporal import RasterGrid
from src.geo.zonal import ZoneStatistics, align_categorical_to_grid, rasterize_categories, zonal_statistics


@dataclass(frozen=True)
class LayerJoinResult:
    descriptor: ExternalLayerDescriptor
    temporal: TemporalJoinAssessment
    zones: tuple[ZoneStatistics, ...]
    layer_coverage_pixels: int  # Valid S2 cells inside the AOI with a layer value.
    unmatched_pixels: int       # Valid S2 cells inside the AOI without a layer value.
    attribution: str
    scope: str = "Statistiques descriptives par classe ; la classe externe n'explique pas à elle seule le NDVI."


def _prepare(grid, values, valid, aoi_path):
    if np.shape(values) != (grid.height, grid.width) or np.shape(valid) != np.shape(values):
        raise ValueError("Les valeurs Sentinel-2 ne correspondent pas à la grille.")
    aoi_pixels = _rasterize_aoi_pixel_mask(grid, aoi_path)
    weights = prepare_pixel_area_weights(grid, load_equal_area_aoi(aoi_path))
    return np.asarray(valid, bool) & aoi_pixels, weights


def _result(descriptor, observed_at, tolerance_days, values, valid, zones, layer_valid, labels, weights):
    stats = zonal_statistics(values, valid, zones, labels, pixel_area_weights=weights)
    return LayerJoinResult(
        descriptor, assess_temporal_join(descriptor.temporal, observed_at, tolerance_days), stats,
        int(np.count_nonzero(valid & layer_valid)), int(np.count_nonzero(valid & ~layer_valid)),
        descriptor.license.attribution)


def join_categorical_raster(grid: RasterGrid, values: np.ndarray, valid: np.ndarray, *, observed_at: datetime,
                            layer_path: str | Path, descriptor: ExternalLayerDescriptor, aoi_path: str | Path,
                            tolerance_days: float = 365.0) -> LayerJoinResult:
    return join_categorical_raster_with_zones(
        grid, values, valid, observed_at=observed_at, layer_path=layer_path, descriptor=descriptor,
        aoi_path=aoi_path, tolerance_days=tolerance_days)[0]


def join_categorical_raster_with_zones(grid: RasterGrid, values: np.ndarray, valid: np.ndarray, *,
                                       observed_at: datetime, layer_path: str | Path,
                                       descriptor: ExternalLayerDescriptor, aoi_path: str | Path,
                                       tolerance_days: float = 365.0, pixel_area_weights=None,
                                       resampling=None):
    """Same join, also returning the aligned (codes, layer_valid) for display and filters."""
    from rasterio.enums import Resampling

    if descriptor.kind != "categorical_raster" or not descriptor.classes:
        raise ValueError("Descripteur de raster catégoriel avec classes requis.")
    if pixel_area_weights is None:
        valid, weights = _prepare(grid, values, valid, aoi_path)
    else:
        if np.shape(values) != (grid.height, grid.width) or np.shape(valid) != np.shape(values):
            raise ValueError("Les valeurs ne correspondent pas à la grille.")
        valid = np.asarray(valid, bool) & _rasterize_aoi_pixel_mask(grid, aoi_path)
        weights = pixel_area_weights
    zones, layer_valid = align_categorical_to_grid(layer_path, grid, resampling=resampling or Resampling.nearest)
    return (_result(descriptor, observed_at, tolerance_days, values, valid, zones, layer_valid,
                    dict(descriptor.classes), weights), zones, layer_valid)


def join_vector_categories(grid: RasterGrid, values: np.ndarray, valid: np.ndarray, *, observed_at: datetime,
                           frame, descriptor: ExternalLayerDescriptor, aoi_path: str | Path,
                           tolerance_days: float = 365.0, all_touched: bool = False) -> LayerJoinResult:
    if descriptor.kind != "vector":
        raise ValueError("Descripteur vecteur requis.")
    valid, weights = _prepare(grid, values, valid, aoi_path)
    categories = sorted(set(frame["category"]))
    codes = {category: index + 1 for index, category in enumerate(categories)}
    zones = rasterize_categories(frame, grid, codes, all_touched=all_touched)
    return _result(descriptor, observed_at, tolerance_days, values, valid, zones, zones > 0,
                   {code: category for category, code in codes.items()}, weights)
