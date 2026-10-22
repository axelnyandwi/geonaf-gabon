"""NDVI of one Sentinel-2 acquisition on its native AOI grid; no UI, no network.

Same chain as the NDVI series (``prepare_date_ndvi``): AOI clip, BOA
calibration from the product XML, NDVI, profile SCL policy, values outside
[-1, 1] rejected. The grid is the clipped 10 m grid of the acquisition itself,
so external layers are aligned onto a measured grid, never the reverse.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np

from src.data.acquisition import Sentinel2Acquisition
from src.geo.change_analysis import (
    _rasterize_aoi_pixel_mask, load_equal_area_aoi, measure_mask_coverage, prepare_pixel_area_weights,
)
from src.geo.scientific_profile import ScientificProfile
from src.geo.temporal import RasterGrid, _read_ndvi_on_grid, grid_from_raster, scl_policy_mask_on_grid
from src.services.ndvi_time_series import NDVISummary, _summary, prepare_date_ndvi
from src.services.temporal_analysis import _profile_context, _revalidate, _utc


@dataclass(frozen=True)
class NDVIObservation:
    """Measured NDVI of one date; ``valid`` already excludes the exterior of the AOI."""

    product_id: str
    acquired_at_utc: datetime
    grid: RasterGrid
    ndvi: np.ndarray          # float32, NaN where invalid.
    valid: np.ndarray         # bool: inside AOI, SCL policy, raster validity, NDVI in [-1, 1].
    aoi_pixels: np.ndarray    # bool: cells touched by the AOI.
    pixel_area_weights: np.ndarray
    scl_policy: str
    valid_coverage_percent: float
    valid_area_m2: float
    summary: NDVISummary
    calibration: dict
    aoi_sha256: str
    scientific_profile: ScientificProfile
    source: str = "Copernicus Sentinel-2 L2A (B04, B08 à 10 m ; SCL à 20 m ré-échantillonné au plus proche voisin)"


def analyze_ndvi_observation(acquisition: Sentinel2Acquisition, *, aoi_path: str | Path,
                             scientific_profile: ScientificProfile) -> NDVIObservation:
    if not isinstance(scientific_profile, ScientificProfile):
        raise TypeError("Un ScientificProfile est requis.")
    aoi_path = Path(aoi_path)
    acquisition = _profile_context(_revalidate(acquisition), scientific_profile)
    policy = scientific_profile.scl_policy
    with TemporaryDirectory(prefix="geonaf-ndvi-date-") as scratch:
        ndvi_path, scl_path = prepare_date_ndvi(acquisition, aoi_path, Path(scratch), "date")
        grid = grid_from_raster(ndvi_path)
        values, raster_valid = _read_ndvi_on_grid(ndvi_path, grid, scl_path, policy)
        scl_valid = scl_policy_mask_on_grid(scl_path, grid, policy)
    aoi_pixels = _rasterize_aoi_pixel_mask(grid, aoi_path)
    weights = prepare_pixel_area_weights(grid, load_equal_area_aoi(aoi_path))
    valid = raster_valid & scl_valid & np.isfinite(values) & (values >= -1.0) & (values <= 1.0) & aoi_pixels
    values = np.where(valid, values, np.nan).astype(np.float32)
    coverage = measure_mask_coverage(valid, grid, load_equal_area_aoi(aoi_path), pixel_area_weights=weights)
    calibration = acquisition.calibration
    return NDVIObservation(
        product_id=acquisition.product_id, acquired_at_utc=_utc(acquisition.acquired_at), grid=grid,
        ndvi=values, valid=valid, aoi_pixels=aoi_pixels, pixel_area_weights=weights, scl_policy=policy,
        valid_coverage_percent=coverage.coverage_percent, valid_area_m2=coverage.area_m2,
        summary=_summary(values[valid]),
        calibration={"quantification_value": calibration.quantification_value,
                     "red_offset": calibration.red_offset, "nir_offset": calibration.nir_offset},
        aoi_sha256=hashlib.sha256(aoi_path.read_bytes()).hexdigest(), scientific_profile=scientific_profile)
