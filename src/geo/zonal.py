"""Spatial joins of external layers with a Sentinel-2 scientific grid.

Categorical rasters use nearest-neighbour alignment (never interpolated
classes); only the source window covering the target grid is read. Vectors
are rasterized on the same grid. Zonal statistics reuse the exact AOI/cell
area weights of ``change_analysis`` when supplied.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import ceil, floor
from typing import Mapping

import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.features import rasterize
from rasterio.warp import reproject, transform_bounds
from rasterio.windows import Window, from_bounds as window_from_bounds

from src.geo.temporal import RasterGrid


@dataclass(frozen=True)
class ZoneStatistics:
    code: int
    label: str
    pixel_count: int
    area_m2: float | None
    mean: float | None
    median: float | None


def align_categorical_to_grid(path, grid: RasterGrid, *, nodata: int = 0,
                              resampling: Resampling = Resampling.nearest) -> tuple[np.ndarray, np.ndarray]:
    """Return (codes uint16, valid) on ``grid``; outside footprint/NoData is invalid.

    ``nearest`` when the target is as fine as the source; ``mode`` (majority
    class) when aggregating onto a coarser grid, e.g. 10 m classes on 20 m radar.
    """
    if resampling not in (Resampling.nearest, Resampling.mode):
        raise ValueError("Ré-échantillonnage catégoriel : plus proche voisin ou mode uniquement.")
    with rasterio.open(path) as source:
        if source.crs is None or source.count != 1:
            raise ValueError("Raster catégoriel mono-bande avec CRS requis.")
        bounds = grid.bounds if source.crs == grid.crs else transform_bounds(
            grid.crs, source.crs, *grid.bounds, densify_pts=21)
        raw = window_from_bounds(*bounds, transform=source.transform)
        col0 = max(0, int(floor(raw.col_off)) - 1)
        row0 = max(0, int(floor(raw.row_off)) - 1)
        col1 = min(source.width, int(ceil(raw.col_off + raw.width)) + 1)
        row1 = min(source.height, int(ceil(raw.row_off + raw.height)) + 1)
        codes = np.zeros((grid.height, grid.width), dtype=np.uint16)
        valid = np.zeros((grid.height, grid.width), dtype=np.uint8)
        if col1 <= col0 or row1 <= row0:
            return codes, valid.astype(bool)
        window = Window(col0, row0, col1 - col0, row1 - row0)
        data = source.read(1, window=window).astype(np.uint16)
        source_valid = (data != nodata).astype(np.uint8)
        if source.nodata is not None:
            source_valid &= (data != source.nodata).astype(np.uint8)
        transform = source.window_transform(window)
        for array, destination in ((data, codes), (source_valid, valid)):
            reproject(source=array, destination=destination, src_transform=transform, src_crs=source.crs,
                      dst_transform=grid.transform, dst_crs=grid.crs, resampling=resampling,
                      init_dest_nodata=True, dst_nodata=0)
    valid = valid.astype(bool)
    codes[~valid] = 0
    return codes, valid


def rasterize_categories(frame, grid: RasterGrid, category_codes: Mapping[str, int], *,
                         column: str = "category", all_touched: bool = False) -> np.ndarray:
    """Burn vector categories (code > 0) on ``grid``; later rows overwrite earlier ones."""
    if frame.crs is None:
        raise ValueError("La couche vecteur doit avoir un CRS.")
    projected = frame.to_crs(grid.crs)
    shapes = [(geometry, category_codes[value]) for geometry, value in zip(projected.geometry, projected[column])
              if value in category_codes and geometry is not None and not geometry.is_empty]
    if not shapes:
        return np.zeros((grid.height, grid.width), dtype=np.uint16)
    return rasterize(shapes, out_shape=(grid.height, grid.width), transform=grid.transform, fill=0,
                     all_touched=all_touched, dtype="uint16")


def zonal_statistics(values: np.ndarray, valid: np.ndarray, zones: np.ndarray, labels: Mapping[int, str], *,
                     pixel_area_weights: np.ndarray | None = None) -> tuple[ZoneStatistics, ...]:
    """Per-zone pixel count, area and NDVI mean/median over ``valid`` cells only."""
    shape = np.shape(values)
    if np.shape(valid) != shape or np.shape(zones) != shape or (
            pixel_area_weights is not None and np.shape(pixel_area_weights) != shape):
        raise ValueError("Tableaux de zonage incompatibles avec la grille.")
    usable = np.asarray(valid, bool) & np.isfinite(values)
    output = []
    for code, label in sorted(labels.items()):
        mask = usable & (zones == code)
        selected = np.asarray(values[mask], dtype=np.float64)
        area = None if pixel_area_weights is None else float(pixel_area_weights[mask].sum(dtype=np.float64))
        output.append(ZoneStatistics(
            int(code), label, int(selected.size), area,
            float(selected.mean()) if selected.size else None,
            float(np.median(selected)) if selected.size else None))
    return tuple(output)
