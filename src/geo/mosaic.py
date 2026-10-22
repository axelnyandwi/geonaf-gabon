"""Mosaics of several tiles already aligned on one target grid; no I/O, no UI.

Rules (identical for every source):

* each tile is first aligned onto the target grid by the source-specific
  helper (mask-aware bilinear for NDVI, nearest/mode for classes, nearest for
  radar); cells outside a tile footprint or masked stay invalid;
* tiles are given in an explicit priority order (native CRS first, then a
  stable identifier). A cell takes the value of the first tile valid there;
  nothing is averaged, interpolated across tiles or filled;
* overlaps are measured: number of cells valid in several tiles, conflicting
  classes or maximum absolute difference of continuous values. They are
  reported, never hidden;
* each tile keeps its own provenance (``TileContribution``) and the mosaic
  records, per cell, which tile supplied the value (``source``).
"""

from __future__ import annotations

from dataclasses import dataclass
from math import ceil, floor
from typing import Sequence

import numpy as np
from affine import Affine
from rasterio.warp import transform_bounds

from src.geo.temporal import RasterGrid


@dataclass(frozen=True)
class AlignedTile:
    tile_id: str
    crs: str
    resolution: tuple[float, float]
    native: bool                  # Same CRS and lattice as the target grid (no resampling).
    values: np.ndarray
    valid: np.ndarray


@dataclass(frozen=True)
class TileContribution:
    tile_id: str
    crs: str
    resolution: tuple[float, float]
    native: bool
    valid_cells: int              # Valid cells of this tile on the target grid.
    selected_cells: int           # Cells whose value comes from this tile.
    overlap_cells: int            # Valid cells already supplied by a higher-priority tile.


@dataclass(frozen=True)
class Mosaic:
    values: np.ndarray
    valid: np.ndarray
    source: np.ndarray            # int16 index into ``contributions``; −1 where no tile is valid.
    contributions: tuple[TileContribution, ...]
    overlap_cells: int
    conflicting_cells: int        # Categorical: different classes; continuous: |Δ| > tolerance.
    max_abs_difference: float | None


def priority_order(tiles: Sequence[AlignedTile]) -> list[AlignedTile]:
    """Native tiles first, then by identifier: deterministic and documented."""
    return sorted(tiles, key=lambda tile: (not tile.native, tile.tile_id))


def mosaic_tiles(tiles: Sequence[AlignedTile], *, categorical: bool, tolerance: float = 1e-6) -> Mosaic:
    if not tiles:
        raise ValueError("Au moins une tuile est requise.")
    shape = np.shape(tiles[0].values)
    if any(np.shape(t.values) != shape or np.shape(t.valid) != shape for t in tiles):
        raise ValueError("Tuiles non alignées sur la même grille.")
    if len({t.tile_id for t in tiles}) != len(tiles):
        raise ValueError("Identifiants de tuiles en double.")
    ordered = priority_order(tiles)
    values = np.zeros(shape, dtype=tiles[0].values.dtype) if categorical else np.full(shape, np.nan, np.float32)
    valid = np.zeros(shape, dtype=bool)
    source = np.full(shape, -1, dtype=np.int16)
    contributions, overlap_total, conflicts, max_difference = [], 0, 0, None
    for index, tile in enumerate(ordered):
        tile_valid = np.asarray(tile.valid, bool)
        overlap = tile_valid & valid
        if overlap.any():
            if categorical:
                conflicts += int(np.count_nonzero(tile.values[overlap] != values[overlap]))
            else:
                difference = np.abs(tile.values[overlap].astype(np.float64) - values[overlap].astype(np.float64))
                max_difference = max(float(difference.max()), max_difference or 0.0)
                conflicts += int(np.count_nonzero(difference > tolerance))
        take = tile_valid & ~valid
        values[take] = tile.values[take]
        valid |= take
        source[take] = index
        overlap_total += int(np.count_nonzero(overlap))
        contributions.append(TileContribution(tile.tile_id, tile.crs, tuple(tile.resolution), tile.native,
                                              int(np.count_nonzero(tile_valid)), int(np.count_nonzero(take)),
                                              int(np.count_nonzero(overlap))))
    return Mosaic(values, valid, source, tuple(contributions), overlap_total, conflicts, max_difference)


def aoi_grid(wgs84_or_native_bounds, bounds_crs, crs, resolution: tuple[float, float],
             origin: tuple[float, float]) -> RasterGrid:
    """Grid covering the bounds in ``crs``, snapped outward to the lattice through ``origin``."""
    import rasterio

    crs = rasterio.crs.CRS.from_user_input(crs)
    bounds = wgs84_or_native_bounds if rasterio.crs.CRS.from_user_input(bounds_crs) == crs else transform_bounds(
        bounds_crs, crs, *wgs84_or_native_bounds, densify_pts=21)
    xres, yres = resolution
    ox, oy = origin
    col0, col1 = floor((bounds[0] - ox) / xres + 1e-9), ceil((bounds[2] - ox) / xres - 1e-9)
    row0, row1 = floor((oy - bounds[3]) / yres + 1e-9), ceil((oy - bounds[1]) / yres - 1e-9)
    if col1 <= col0 or row1 <= row0:
        raise ValueError("Emprise vide pour la grille cible.")
    transform = Affine.translation(ox + col0 * xres, oy - row0 * yres) @ Affine.scale(xres, -yres)
    return RasterGrid(crs, (xres, yres), transform, col1 - col0, row1 - row0)


def same_lattice(transform, grid: RasterGrid, crs) -> bool:
    """True when a raster shares CRS, resolution and cell alignment with ``grid``."""
    if crs != grid.crs or transform.a != grid.transform.a or transform.e != grid.transform.e:
        return False
    dx = (grid.transform.c - transform.c) / transform.a
    dy = (grid.transform.f - transform.f) / transform.e
    return abs(dx - round(dx)) < 1e-9 and abs(dy - round(dy)) < 1e-9
