"""Spatial harmonization for two Sentinel-2 NDVI acquisitions.

The first acquisition defines the default CRS, resolution, and grid origin.
The returned grid is restricted to the inward-snapped intersection of the two
raster footprints. NDVI uses mask-weighted bilinear interpolation; output
pixels are accepted only when the full interpolation weight is valid.
Categorical SCL data uses nearest-neighbour resampling and one shared named
policy. No files, downloads, or model predictions are produced here.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import ceil, floor, isclose
from pathlib import Path

import numpy as np
import rasterio
from affine import Affine
from rasterio.enums import Resampling
from rasterio.windows import Window, from_bounds as window_from_bounds
from rasterio.warp import reproject, transform_bounds

from src.data.cloud_mask import scl_mask_from_values


@dataclass(frozen=True)
class RasterGrid:
    """Complete description of a north-up raster grid."""

    crs: rasterio.crs.CRS
    resolution: tuple[float, float]
    transform: Affine
    width: int
    height: int

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        """Return left, bottom, right, top in ``crs`` units."""
        left, top = self.transform @ (0, 0)
        right, bottom = self.transform @ (self.width, self.height)
        return (left, bottom, right, top)


@dataclass(frozen=True)
class TemporalComparison:
    """NDVI arrays and masks aligned to one common grid.

    ``comparison_mask`` is true only where both NDVI observations survive
    raster validity and the same SCL policy. ``delta_ndvi`` is always
    date 2 minus date 1 and is NaN elsewhere.
    """

    grid: RasterGrid
    ndvi_date_1: np.ndarray
    ndvi_date_2: np.ndarray
    valid_date_1: np.ndarray
    valid_date_2: np.ndarray
    comparison_mask: np.ndarray
    delta_ndvi: np.ndarray


def _north_up_metadata(path: str | Path) -> tuple[dict, rasterio.crs.CRS]:
    with rasterio.open(path) as source:
        if source.crs is None:
            raise ValueError("CRS manquant sur un raster.")
        if source.count != 1:
            raise ValueError("Chaque raster doit contenir une seule bande.")
        if (
            not isclose(source.transform.b, 0.0, abs_tol=1e-12)
            or not isclose(source.transform.d, 0.0, abs_tol=1e-12)
            or source.transform.a <= 0
            or source.transform.e >= 0
        ):
            raise ValueError("Seules les grilles raster orientées nord et sans rotation sont prises en charge.")
        metadata = {
            "crs": source.crs,
            "transform": source.transform,
            "width": source.width,
            "height": source.height,
            "bounds": tuple(source.bounds),
            "resolution": (abs(source.transform.a), abs(source.transform.e)),
        }
    return metadata, metadata["crs"]


def _inward_grid_window(
    bounds: tuple[float, float, float, float],
    origin_x: float,
    origin_y: float,
    resolution: tuple[float, float],
) -> tuple[int, int, int, int]:
    """Snap bounds inward to complete cells; return col/row start and size."""
    left, bottom, right, top = bounds
    x_resolution, y_resolution = resolution
    tolerance = 1e-10

    col_start = ceil((left - origin_x) / x_resolution - tolerance)
    col_stop = floor((right - origin_x) / x_resolution + tolerance)
    row_start = ceil((origin_y - top) / y_resolution - tolerance)
    row_stop = floor((origin_y - bottom) / y_resolution + tolerance)
    return col_start, row_start, col_stop - col_start, row_stop - row_start


def build_common_grid(
    ndvi_date_1_path: str | Path,
    ndvi_date_2_path: str | Path,
    *,
    target_crs: str | rasterio.crs.CRS | None = None,
    resolution: tuple[float, float] | None = None,
) -> RasterGrid:
    """Build an inward-snapped grid over two single-band raster footprints.

    By default, the first raster supplies the target CRS, resolution, and pixel origin.
    If the inputs already have the same grid, that exact grid is returned,
    avoiding warp/resampling. A different ``target_crs`` or ``resolution``
    explicitly requests a new grid anchored at date 1's transformed upper-left
    corner. Resolutions are expressed in target CRS units.

    The footprint intersection uses densified transformed bounds. Pixel cells
    are snapped inward, so no output cell extends beyond that rectangular
    intersection. Rotated/sheared grids are rejected rather than approximated.
    """
    first, _ = _north_up_metadata(ndvi_date_1_path)
    second, _ = _north_up_metadata(ndvi_date_2_path)

    same_crs = first["crs"] == second["crs"]
    same_grid = (
        same_crs
        and first["transform"] == second["transform"]
        and first["width"] == second["width"]
        and first["height"] == second["height"]
    )
    if same_grid and target_crs is None and resolution is None:
        return RasterGrid(
            crs=first["crs"],
            resolution=first["resolution"],
            transform=first["transform"],
            width=first["width"],
            height=first["height"],
        )

    output_crs = rasterio.crs.CRS.from_user_input(target_crs or first["crs"])
    output_resolution = resolution or first["resolution"]
    if (
        len(output_resolution) != 2
        or not all(np.isfinite(value) and value > 0 for value in output_resolution)
    ):
        raise ValueError("La résolution cible doit contenir deux valeurs finies et positives.")
    output_resolution = (float(output_resolution[0]), float(output_resolution[1]))

    if output_crs == first["crs"]:
        first_bounds = first["bounds"]
    else:
        first_bounds = transform_bounds(first["crs"], output_crs, *first["bounds"])
    second_bounds = transform_bounds(
        second["crs"], output_crs, *second["bounds"]
    )

    left = max(first_bounds[0], second_bounds[0])
    bottom = max(first_bounds[1], second_bounds[1])
    right = min(first_bounds[2], second_bounds[2])
    top = min(first_bounds[3], second_bounds[3])
    if left >= right or bottom >= top:
        raise ValueError("Les deux rasters n'ont aucune intersection spatiale.")

    if output_crs == first["crs"] and output_resolution == first["resolution"]:
        origin_x, origin_y = first["transform"].c, first["transform"].f
    else:
        origin_x, origin_y = first_bounds[0], first_bounds[3]

    col_start, row_start, width, height = _inward_grid_window(
        (left, bottom, right, top),
        origin_x,
        origin_y,
        output_resolution,
    )
    if width <= 0 or height <= 0:
        raise ValueError("L'intersection est trop petite pour contenir une cellule cible.")

    output_transform = (
        Affine.translation(origin_x, origin_y)
        @ Affine.scale(output_resolution[0], -output_resolution[1])
        @ Affine.translation(col_start, row_start)
    )
    return RasterGrid(
        crs=output_crs,
        resolution=output_resolution,
        transform=output_transform,
        width=width,
        height=height,
    )


def _is_same_grid(source, grid: RasterGrid) -> bool:
    return (
        source.crs == grid.crs
        and source.transform == grid.transform
        and source.width == grid.width
        and source.height == grid.height
    )


def _validate_grid(grid: RasterGrid) -> None:
    """Reject inconsistent or unsupported caller-supplied reference grids."""
    if not isinstance(grid, RasterGrid):
        raise TypeError("grid doit être une instance de RasterGrid.")
    if grid.crs is None:
        raise ValueError("CRS de la grille de comparaison manquant.")
    if grid.width <= 0 or grid.height <= 0:
        raise ValueError("La grille de comparaison doit avoir des dimensions positives.")
    if (
        len(grid.resolution) != 2
        or not all(np.isfinite(value) and value > 0 for value in grid.resolution)
    ):
        raise ValueError("La résolution de la grille doit contenir deux valeurs finies et positives.")
    if (
        not isclose(grid.transform.b, 0.0, abs_tol=1e-12)
        or not isclose(grid.transform.d, 0.0, abs_tol=1e-12)
        or grid.transform.a <= 0
        or grid.transform.e >= 0
    ):
        raise ValueError("La grille de comparaison doit être orientée nord et sans rotation.")
    if not (
        isclose(grid.transform.a, grid.resolution[0], rel_tol=1e-9)
        and isclose(abs(grid.transform.e), grid.resolution[1], rel_tol=1e-9)
    ):
        raise ValueError("La résolution ne correspond pas à la transformée affine de la grille.")


def _aligned_integer_window(source, grid: RasterGrid) -> tuple[int, int] | None:
    """Return an exact source window offset when grids differ only by extent."""
    if source.crs != grid.crs:
        return None
    if (
        source.transform.a != grid.transform.a
        or source.transform.e != grid.transform.e
        or not isclose(source.transform.b, grid.transform.b, abs_tol=1e-12)
        or not isclose(source.transform.d, grid.transform.d, abs_tol=1e-12)
    ):
        return None
    col_offset = (grid.transform.c - source.transform.c) / source.transform.a
    row_offset = (grid.transform.f - source.transform.f) / source.transform.e
    col, row = round(col_offset), round(row_offset)
    if not isclose(col_offset, col, abs_tol=1e-9) or not isclose(
        row_offset, row, abs_tol=1e-9
    ):
        return None
    if (
        col < 0
        or row < 0
        or col + grid.width > source.width
        or row + grid.height > source.height
    ):
        return None
    return col, row


def _read_ndvi_on_grid(
    path: str | Path, grid: RasterGrid, scl_path: str | Path, scl_policy: str
) -> tuple[np.ndarray, np.ndarray]:
    with rasterio.open(path) as source:
        if source.crs is None:
            raise ValueError("CRS manquant sur un raster NDVI.")
        if source.count != 1:
            raise ValueError("Chaque raster NDVI doit contenir une seule bande.")

        source_data = source.read(1, masked=True)
        source_values = np.asarray(np.ma.getdata(source_data), dtype=np.float32)
        source_valid = ~np.ma.getmaskarray(source_data) & np.isfinite(source_values)

        if _is_same_grid(source, grid):
            return source_values.copy(), source_valid.copy()

        exact_window = _aligned_integer_window(source, grid)
        if exact_window is not None:
            col, row = exact_window
            window = rasterio.windows.Window(col, row, grid.width, grid.height)
            window_data = source.read(1, window=window, masked=True)
            values = np.asarray(np.ma.getdata(window_data), dtype=np.float32)
            valid = ~np.ma.getmaskarray(window_data) & np.isfinite(values)
            return values, valid

        # Classify on the NDVI source grid BEFORE any continuous interpolation.
        # Nearest-neighbour SCL alignment does not increase its native precision.
        source_grid = RasterGrid(
            source.crs, source.res, source.transform, source.width, source.height
        )
        source_valid &= scl_policy_mask_on_grid(scl_path, source_grid, scl_policy)
        # Explicit invalid padding prevents GDAL from normalizing a truncated
        # edge kernel into full validity. Zero weights are data, not src_nodata.
        numerator_source = np.pad(
            np.where(source_valid, source_values, 0).astype(np.float32), 1
        )
        weight_source = np.pad(source_valid.astype(np.float32), 1)
        source_transform = source.transform @ Affine.translation(-1, -1)
        numerator = np.zeros((grid.height, grid.width), dtype=np.float32)
        weights = np.zeros((grid.height, grid.width), dtype=np.float32)

        for source_array, destination in (
            (numerator_source, numerator),
            (weight_source, weights),
        ):
            reproject(
                source=source_array,
                destination=destination,
                src_transform=source_transform,
                src_crs=source.crs,
                dst_transform=grid.transform,
                dst_crs=grid.crs,
                resampling=Resampling.bilinear,
                init_dest_nodata=True,
                dst_nodata=0,
            )

    # Require the complete bilinear kernel to come from valid source pixels.
    # This avoids normalizing a partial neighborhood into a seemingly valid
    # observation at a NoData/SCL exclusion or footprint edge. Accept only
    # weight >= 1 - 1e-6 (floating-point warp tolerance); never fill a partial
    # neighbourhood by renormalizing its remaining valid observations.
    valid = weights >= (1.0 - 1e-6)
    values = np.full((grid.height, grid.width), np.nan, dtype=np.float32)
    np.divide(numerator, weights, out=values, where=valid & (weights > 0))
    valid &= np.isfinite(values)
    values[~valid] = np.nan
    return values, valid


def scl_policy_mask_on_grid(
    path: str | Path,
    grid: RasterGrid,
    policy: str,
) -> np.ndarray:
    """Read, nearest-neighbour align, and classify SCL on a reference grid.

    Source NoData and pixels outside the source footprint stay invalid. This
    helper is shared by temporal NDVI comparison and pre-analysis pair scoring.
    """
    with rasterio.open(path) as source:
        if source.crs is None:
            raise ValueError("CRS manquant sur un raster SCL.")
        if source.count != 1:
            raise ValueError("Chaque raster SCL doit contenir une seule bande.")
        if _is_same_grid(source, grid):
            source_scl = source.read(1, masked=True)
            source_values = np.asarray(np.ma.getdata(source_scl), dtype=np.uint8)
            source_valid = ~np.ma.getmaskarray(source_scl)
            return scl_mask_from_values(
                np.ma.array(source_values, mask=~source_valid),
                policy=policy,
                valid_mask=source_valid,
            )

        exact_window = _aligned_integer_window(source, grid)
        if exact_window is not None:
            col, row = exact_window
            window = Window(col, row, grid.width, grid.height)
            source_scl = source.read(1, window=window, masked=True)
            source_values = np.asarray(np.ma.getdata(source_scl), dtype=np.uint8)
            source_valid = ~np.ma.getmaskarray(source_scl)
            return scl_mask_from_values(
                np.ma.array(source_values, mask=~source_valid),
                policy=policy,
                valid_mask=source_valid,
            )

        # For a warp, read only the source window intersecting the target
        # bounds, with a one-cell margin for nearest-neighbour edge sampling.
        # Rotated grids retain the established full-read fallback because a
        # rectangular bounds window is not sufficient to describe them.
        if isclose(source.transform.b, 0.0, abs_tol=1e-12) and isclose(
            source.transform.d, 0.0, abs_tol=1e-12
        ):
            bounds = (
                grid.bounds
                if source.crs == grid.crs
                else transform_bounds(
                    grid.crs, source.crs, *grid.bounds, densify_pts=21
                )
            )
            raw_window = window_from_bounds(*bounds, transform=source.transform)
            col_start = max(0, int(floor(raw_window.col_off)) - 1)
            row_start = max(0, int(floor(raw_window.row_off)) - 1)
            col_stop = min(
                source.width, int(ceil(raw_window.col_off + raw_window.width)) + 1
            )
            row_stop = min(
                source.height, int(ceil(raw_window.row_off + raw_window.height)) + 1
            )
            if col_stop <= col_start or row_stop <= row_start:
                return np.zeros((grid.height, grid.width), dtype=bool)
            source_window = Window(
                col_start, row_start, col_stop - col_start, row_stop - row_start
            )
            source_scl = source.read(1, window=source_window, masked=True)
            source_transform = source.window_transform(source_window)
        else:
            source_scl = source.read(1, masked=True)
            source_transform = source.transform
        source_values = np.asarray(np.ma.getdata(source_scl), dtype=np.uint8)
        source_valid = ~np.ma.getmaskarray(source_scl)
        aligned_scl = np.zeros((grid.height, grid.width), dtype=np.uint8)
        aligned_valid = np.zeros((grid.height, grid.width), dtype=np.uint8)
        for source_array, destination in (
            (source_values, aligned_scl),
            (source_valid.astype(np.uint8), aligned_valid),
        ):
            reproject(
                source=source_array,
                destination=destination,
                src_transform=source_transform,
                src_crs=source.crs,
                dst_transform=grid.transform,
                dst_crs=grid.crs,
                resampling=Resampling.nearest,
                init_dest_nodata=True,
                dst_nodata=0,
            )
        aligned_valid = aligned_valid.astype(bool)

    return scl_mask_from_values(
        np.ma.array(aligned_scl, mask=~aligned_valid),
        policy=policy,
        valid_mask=aligned_valid,
    )


def harmonize_ndvi_pair(
    ndvi_date_1_path: str | Path,
    ndvi_date_2_path: str | Path,
    scl_date_1_path: str | Path,
    scl_date_2_path: str | Path,
    *,
    grid: RasterGrid | None = None,
    scl_policy: str = "usable_land",
) -> TemporalComparison:
    """Align two NDVI/SCL dates and calculate their common-mask difference.

    NDVI is expected to be already calibrated reflectance-derived data; this
    function never applies Sentinel-2 radiometric offsets. The default common
    grid uses date 1's CRS, resolution, and origin over the inward-snapped
    footprint intersection. SCL uses nearest-neighbour resampling. The same
    policy (from ``src.data.cloud_mask.SCL_MASK_POLICIES``) is applied to both
    dates. ``delta_ndvi`` is ``ndvi_date_2 - ndvi_date_1`` and is NaN outside
    the common valid mask.

    For interpolated NDVI, SCL is first aligned by nearest neighbour to each
    NDVI source grid. Excluded source values contribute neither numerator nor
    validity weight. The full bilinear weight must be valid (tolerance 1e-6),
    including at NoData and footprint edges; otherwise the output is NaN.
    Destination SCL remains an additional nearest-neighbour check. This does
    not infer SCL detail at 10 m from a coarser classification. Identical grids
    and exact integer windows retain their values without interpolation.
    """
    if grid is None:
        grid = build_common_grid(ndvi_date_1_path, ndvi_date_2_path)
    else:
        _validate_grid(grid)

    ndvi_1, raster_valid_1 = _read_ndvi_on_grid(
        ndvi_date_1_path, grid, scl_date_1_path, scl_policy
    )
    ndvi_2, raster_valid_2 = _read_ndvi_on_grid(
        ndvi_date_2_path, grid, scl_date_2_path, scl_policy
    )
    scl_valid_1 = scl_policy_mask_on_grid(scl_date_1_path, grid, scl_policy)
    scl_valid_2 = scl_policy_mask_on_grid(scl_date_2_path, grid, scl_policy)

    valid_1 = raster_valid_1 & scl_valid_1
    valid_2 = raster_valid_2 & scl_valid_2
    comparison_mask = valid_1 & valid_2

    ndvi_1 = ndvi_1.astype(np.float32, copy=True)
    ndvi_2 = ndvi_2.astype(np.float32, copy=True)
    ndvi_1[~valid_1] = np.nan
    ndvi_2[~valid_2] = np.nan
    delta_ndvi = np.full((grid.height, grid.width), np.nan, dtype=np.float32)
    np.subtract(
        ndvi_2,
        ndvi_1,
        out=delta_ndvi,
        where=comparison_mask,
    )

    return TemporalComparison(
        grid=grid,
        ndvi_date_1=ndvi_1,
        ndvi_date_2=ndvi_2,
        valid_date_1=valid_1,
        valid_date_2=valid_2,
        comparison_mask=comparison_mask,
        delta_ndvi=delta_ndvi,
    )
