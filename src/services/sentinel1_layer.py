"""Radar layer of a stored Sentinel-1 mosaic window; no UI, no network.

The radar stays on its native 20 m grid (no resampling of backscatter). Land
cover is aggregated onto that grid by majority class when per-class
statistics are requested. Radar values are never compared with NDVI: they
measure surface roughness/structure and moisture, not chlorophyll activity.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import rasterio
from rasterio.enums import Resampling

from src.data.external_sources import ExternalLayerDescriptor
from src.data.sentinel1 import SCALE_CONVENTIONS
from src.geo.change_analysis import _rasterize_aoi_pixel_mask
from src.geo.temporal import RasterGrid
from src.geo.zonal import ZoneStatistics, align_categorical_to_grid, zonal_statistics

# Deduced on Akanda (Jan. 2025) from class medians: 10·log10 gives forest VV ≈ −6.6 dB,
# water VH ≈ −30 dB (expected C-band orders); 20·log10 would give water VH ≈ −60 dB.
DEFAULT_SCALE = "intensity"
DISPLAY_RANGE_DB = {"VV": (-25.0, 0.0), "VH": (-32.0, -7.0)}


@dataclass(frozen=True)
class RadarLayer:
    polarization: str
    descriptor: ExternalLayerDescriptor
    grid: RasterGrid
    db: np.ndarray            # float32 dB, NaN where invalid.
    valid: np.ndarray         # bool: inside AOI, finite, > 0, not NoData.
    aoi_pixels: np.ndarray
    scale: str
    invalid_counts: dict
    median_db: float | None
    p25_db: float | None
    p75_db: float | None
    coverage_percent: float   # Valid cells / AOI cells (cell count, not exact area).
    per_class: tuple[ZoneStatistics, ...] = ()

    @property
    def unit(self) -> str:
        return f"dB (10·log10 de la valeur linéaire, convention « {self.scale} »)" if self.scale == "intensity" \
            else f"dB (20·log10, convention « {self.scale} »)"


def load_radar_layer(path: str | Path, descriptor: ExternalLayerDescriptor, *, aoi_path: str | Path,
                     scale: str = DEFAULT_SCALE, landcover_path: str | Path | None = None,
                     landcover_classes: dict | None = None) -> RadarLayer:
    if scale not in SCALE_CONVENTIONS:
        raise ValueError("Convention d'échelle radar inconnue.")
    polarization = descriptor.provenance.get("polarization")
    if descriptor.source_id != "sentinel-1-mosaic" or polarization not in ("VV", "VH"):
        raise ValueError("Descripteur Sentinel-1 VV/VH requis.")
    with rasterio.open(path) as source:
        values = source.read(1).astype(np.float32)
        nodata = source.nodata
        grid = RasterGrid(source.crs, (abs(source.transform.a), abs(source.transform.e)), source.transform,
                          source.width, source.height)
    aoi_pixels = _rasterize_aoi_pixel_mask(grid, aoi_path)
    is_nodata = (values == nodata) if nodata is not None else np.zeros(values.shape, bool)
    not_finite = ~np.isfinite(values)
    not_positive = np.isfinite(values) & (values <= 0) & ~is_nodata
    valid = aoi_pixels & ~is_nodata & ~not_finite & ~not_positive
    db = np.full(values.shape, np.nan, dtype=np.float32)
    db[valid] = SCALE_CONVENTIONS[scale] * np.log10(values[valid])
    selected = db[valid].astype(np.float64)
    p25, median, p75 = (np.percentile(selected, [25, 50, 75]) if selected.size else (None, None, None))
    per_class = ()
    if landcover_path is not None and landcover_classes:
        codes, layer_valid = align_categorical_to_grid(landcover_path, grid, resampling=Resampling.mode)
        per_class = zonal_statistics(db, valid & layer_valid, codes, landcover_classes)
    inside = int(np.count_nonzero(aoi_pixels))
    return RadarLayer(
        polarization=polarization, descriptor=descriptor, grid=grid, db=db, valid=valid, aoi_pixels=aoi_pixels,
        scale=scale,
        invalid_counts={"nodata": int(np.count_nonzero(is_nodata & aoi_pixels)),
                        "non_fini": int(np.count_nonzero(not_finite & aoi_pixels)),
                        "non_positif": int(np.count_nonzero(not_positive & aoi_pixels))},
        median_db=None if median is None else float(median), p25_db=None if p25 is None else float(p25),
        p75_db=None if p75 is None else float(p75),
        coverage_percent=100.0 * np.count_nonzero(valid) / inside if inside else 0.0, per_class=per_class)
