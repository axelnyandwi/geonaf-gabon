"""Change classes and area statistics for a harmonized NDVI date pair."""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path

import numpy as np
from pyproj import Transformer
from shapely import (
    area as geometry_area,
    intersection,
    is_empty,
    is_valid,
    polygons,
    transform as transform_geometry,
    union_all,
)

from src.diagnostics import safe_call
from src.geo.temporal import RasterGrid, TemporalComparison


EQUAL_AREA_CRS = "EPSG:6933"
DEFAULT_CHANGE_THRESHOLD = 0.10
MAX_AREA_CELLS_PER_CHUNK = 100_000


class ChangeClass(IntEnum):
    """Stable integer codes for raster maps consumed by later interfaces."""

    NON_COMPARABLE = 0
    DECREASE = 1
    STABLE = 2
    INCREASE = 3


@dataclass(frozen=True)
class NDVIChangeAnalysis:
    """Raster-ready change classes plus reproducible summary indicators.

    ``coverage_denominator`` is ``"aoi"`` when a vector AOI was supplied;
    otherwise it is ``"comparison_grid"`` and the percentage describes the
    common grid only, not geographic coverage of an AOI.
    """

    change_class_map: np.ndarray
    delta_ndvi: np.ndarray
    comparable_mask: np.ndarray
    statistics: dict[str, int | float | None]
    areas_m2: dict[str, float]
    areas_ha: dict[str, float]
    class_pixel_counts: dict[str, int]
    comparable_area_m2: float
    denominator_area_m2: float
    comparable_coverage_percent: float
    coverage_denominator: str
    threshold: float
    excluded_out_of_range_pixels: int


@dataclass(frozen=True)
class MaskCoverage:
    """Area covered by a raster mask relative to a supplied vector AOI."""

    area_m2: float
    aoi_area_m2: float
    coverage_percent: float
    pixel_count: int


@dataclass(frozen=True)
class EqualAreaAOI:
    """Validated AOI union in ``EQUAL_AREA_CRS`` for repeated measurements."""

    geometry: object
    area_m2: float


def prepare_pixel_area_weights(
    grid: RasterGrid, aoi: EqualAreaAOI
) -> np.ndarray:
    """Prepare exact equal-area AOI intersection weights once per grid.

    Zero-weight cells lie outside the AOI. The returned float64 array is
    reusable for multiple masks on the same affine grid.
    """
    weights = np.zeros((grid.height, grid.width), dtype=np.float64)
    for row_start, row_end, pixel_areas in _pixel_area_chunks(
        grid, aoi.geometry
    ):
        weights[row_start:row_end] = pixel_areas
    return weights


def _load_aoi_equal_area(aoi_path: str | Path):
    """Read, validate, union, and project an AOI without exposing its path."""
    import geopandas as gpd

    aoi = safe_call(gpd.read_file, aoi_path, paths=(aoi_path,))
    if aoi.empty:
        raise ValueError("AOI vide.")
    if aoi.crs is None:
        raise ValueError("L'AOI ne possède pas de CRS.")
    if any(geometry is None or is_empty(geometry) for geometry in aoi.geometry):
        raise ValueError("L'AOI contient une géométrie vide.")
    if not all(is_valid(geometry) for geometry in aoi.geometry):
        raise ValueError("L'AOI contient une géométrie invalide.")

    projected = aoi.to_crs(EQUAL_AREA_CRS)
    geometry = union_all(projected.geometry.array)
    area_m2 = float(geometry_area(geometry))
    if not np.isfinite(area_m2) or area_m2 <= 0:
        raise ValueError("L'aire de l'AOI est nulle ou invalide.")
    return geometry, area_m2


def load_equal_area_aoi(aoi_path: str | Path) -> EqualAreaAOI:
    """Load and validate an AOI once for repeated raster coverage measures."""
    geometry, area_m2 = _load_aoi_equal_area(aoi_path)
    return EqualAreaAOI(geometry=geometry, area_m2=area_m2)


def _pixel_area_chunks(grid: RasterGrid, aoi_geometry=None):
    """Yield exact equal-area pixel intersections in bounded row chunks."""
    transformer = Transformer.from_crs(
        grid.crs,
        EQUAL_AREA_CRS,
        always_xy=True,
    )
    width = grid.width
    rows_per_chunk = max(1, min(128, MAX_AREA_CELLS_PER_CHUNK // width))

    for row_start in range(0, grid.height, rows_per_chunk):
        row_end = min(row_start + rows_per_chunk, grid.height)
        rows, cols = np.meshgrid(
            np.arange(row_start, row_end, dtype=np.float64),
            np.arange(width, dtype=np.float64),
            indexing="ij",
        )
        corner_coordinates = []
        for col_offset, row_offset in (
            (0, 0),
            (1, 0),
            (1, 1),
            (0, 1),
            (0, 0),
        ):
            pixel_cols = cols + col_offset
            pixel_rows = rows + row_offset
            x = (
                grid.transform.a * pixel_cols
                + grid.transform.c
            )
            y = (
                grid.transform.e * pixel_rows
                + grid.transform.f
            )
            corner_coordinates.append(np.stack((x, y), axis=-1))

        cell_coordinates = np.stack(corner_coordinates, axis=2).reshape(-1, 5, 2)
        cell_polygons = polygons(cell_coordinates)
        equal_area_cells = transform_geometry(
            cell_polygons,
            transformer.transform,
            interleaved=False,
        )
        if aoi_geometry is not None:
            equal_area_cells = intersection(equal_area_cells, aoi_geometry)

        pixel_areas = geometry_area(equal_area_cells).reshape(
            row_end - row_start,
            width,
        )
        yield row_start, row_end, pixel_areas


def _cell_area_sums(
    grid: RasterGrid,
    comparable_mask: np.ndarray,
    class_map: np.ndarray,
    aoi_geometry=None,
) -> tuple[float, float, dict[str, float]]:
    """Measure grid, comparable, and per-class areas in equal-area metres."""
    total_grid_area = 0.0
    comparable_area = 0.0
    class_areas = {"decrease": 0.0, "stable": 0.0, "increase": 0.0}

    for row_start, row_end, pixel_areas in _pixel_area_chunks(grid, aoi_geometry):
        total_grid_area += float(pixel_areas.sum())
        comparable_chunk = comparable_mask[row_start:row_end]
        class_chunk = class_map[row_start:row_end]
        comparable_area += float(pixel_areas[comparable_chunk].sum())
        for class_name, class_value in (
            ("decrease", ChangeClass.DECREASE),
            ("stable", ChangeClass.STABLE),
            ("increase", ChangeClass.INCREASE),
        ):
            class_areas[class_name] += float(
                pixel_areas[class_chunk == class_value].sum()
            )

    return total_grid_area, comparable_area, class_areas


def measure_mask_coverage(
    mask: np.ndarray,
    grid: RasterGrid,
    aoi_path: str | Path | EqualAreaAOI,
    *,
    pixel_area_weights: np.ndarray | None = None,
) -> MaskCoverage:
    """Measure mask coverage of an AOI using exact pixel/AOI intersections.

    Cell areas use the same equal-area CRS, AOI union, and bounded chunking as
    :func:`analyze_ndvi_changes`. Partial cells contribute only their area
    inside the AOI.
    """
    mask = np.asarray(mask, dtype=bool)
    expected_shape = (grid.height, grid.width)
    if mask.shape != expected_shape:
        raise ValueError("Le masque ne correspond pas aux dimensions de la grille.")

    if isinstance(aoi_path, EqualAreaAOI):
        aoi_geometry = aoi_path.geometry
        aoi_area_m2 = aoi_path.area_m2
    else:
        aoi_geometry, aoi_area_m2 = _load_aoi_equal_area(aoi_path)
    covered_area_m2 = 0.0
    covered_pixel_count = 0
    if pixel_area_weights is not None:
        weights = np.asarray(pixel_area_weights)
        if weights.shape != expected_shape or not np.issubdtype(weights.dtype, np.number):
            raise ValueError("Les poids surfaciques ne correspondent pas à la grille.")
        if not np.all(np.isfinite(weights)) or np.any(weights < 0):
            raise ValueError("Les poids surfaciques doivent être finis et positifs.")
        # Keep the former row-chunk summation order. Besides bounding temporary
        # arrays, this preserves stable floating-point pair ties against the
        # uncached exact-intersection implementation.
        rows_per_chunk = max(
            1, min(128, MAX_AREA_CELLS_PER_CHUNK // grid.width)
        )
        for row_start in range(0, grid.height, rows_per_chunk):
            row_end = min(row_start + rows_per_chunk, grid.height)
            weight_chunk = weights[row_start:row_end]
            mask_chunk = mask[row_start:row_end]
            covered_area_m2 += float(weight_chunk[mask_chunk].sum(dtype=np.float64))
            covered_pixel_count += int(
                np.count_nonzero(mask_chunk & (weight_chunk > 0))
            )
    else:
        for row_start, row_end, pixel_areas in _pixel_area_chunks(grid, aoi_geometry):
            mask_chunk = mask[row_start:row_end]
            covered_area_m2 += float(pixel_areas[mask_chunk].sum())
            covered_pixel_count += int(np.count_nonzero(mask_chunk & (pixel_areas > 0)))

    coverage_percent = float(
        np.clip(covered_area_m2 / aoi_area_m2 * 100.0, 0.0, 100.0)
    )
    return MaskCoverage(
        area_m2=covered_area_m2,
        aoi_area_m2=aoi_area_m2,
        coverage_percent=coverage_percent,
        pixel_count=covered_pixel_count,
    )


def _summarize_delta(delta: np.ndarray, comparable_mask: np.ndarray) -> dict:
    values = delta[comparable_mask].astype(np.float64)
    if values.size == 0:
        return {
            "count": 0,
            "mean": None,
            "std": None,
            "min": None,
            "p25": None,
            "median": None,
            "p75": None,
            "max": None,
        }

    p25, median, p75 = np.percentile(values, [25, 50, 75])
    return {
        "count": int(values.size),
        "mean": float(np.mean(values)),
        "std": float(np.std(values, ddof=0)),
        "min": float(np.min(values)),
        "p25": float(p25),
        "median": float(median),
        "p75": float(p75),
        "max": float(np.max(values)),
    }


def analyze_ndvi_changes(
    comparison: TemporalComparison,
    *,
    threshold: float = DEFAULT_CHANGE_THRESHOLD,
    aoi_path: str | Path | None = None,
) -> NDVIChangeAnalysis:
    """Classify and summarize NDVI differences from a harmonized date pair.

    The input is the result of :func:`src.geo.temporal.harmonize_ndvi_pair`;
    this function does not align, resample, recalibrate, or download rasters.
    Decrease is ``delta < -threshold``, increase is ``delta > threshold``,
    and both threshold boundaries belong to the stable class. Pixels outside
    the common mask, with non-finite data, or with either NDVI outside
    ``[-1, 1]`` are non-comparable. NDVI range exclusions are counted.

    Areas use EPSG:6933, a global equal-area CRS compatible with GEONAF's AOIs.
    AOI surfaces are weighted by each cell's exact polygon intersection with
    the unioned AOI. Without an AOI, comparable coverage is relative to all
    cells in the common grid and is labelled ``comparison_grid``.
    """
    if isinstance(threshold, (bool, np.bool_)) or not np.isscalar(threshold):
        raise ValueError("Le seuil de changement doit être fini et strictement positif.")
    try:
        threshold = float(threshold)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(
            "Le seuil de changement doit être fini et strictement positif."
        ) from None
    if not np.isfinite(threshold) or threshold <= 0:
        raise ValueError("Le seuil de changement doit être fini et strictement positif.")

    shape = (comparison.grid.height, comparison.grid.width)
    arrays = (
        comparison.ndvi_date_1,
        comparison.ndvi_date_2,
        comparison.delta_ndvi,
        comparison.comparison_mask,
    )
    if any(np.shape(array) != shape for array in arrays):
        raise ValueError("Les tableaux de comparaison ne correspondent pas à la grille.")

    date_1 = np.asarray(comparison.ndvi_date_1, dtype=np.float64)
    date_2 = np.asarray(comparison.ndvi_date_2, dtype=np.float64)
    delta = np.asarray(comparison.delta_ndvi, dtype=np.float64).copy()
    source_comparable = np.asarray(comparison.comparison_mask, dtype=bool)
    in_ndvi_range = (
        np.isfinite(date_1)
        & np.isfinite(date_2)
        & (date_1 >= -1.0)
        & (date_1 <= 1.0)
        & (date_2 >= -1.0)
        & (date_2 <= 1.0)
    )
    excluded_out_of_range = int(
        np.count_nonzero(source_comparable & np.isfinite(date_1) & np.isfinite(date_2) & ~in_ndvi_range)
    )
    comparable_mask = source_comparable & in_ndvi_range & np.isfinite(delta)
    delta[~comparable_mask] = np.nan

    change_map = np.full(shape, ChangeClass.NON_COMPARABLE, dtype=np.uint8)
    change_map[comparable_mask] = ChangeClass.STABLE
    change_map[comparable_mask & (delta < -threshold)] = ChangeClass.DECREASE
    change_map[comparable_mask & (delta > threshold)] = ChangeClass.INCREASE

    aoi_geometry = None
    denominator = "comparison_grid"
    aoi_area_m2 = None
    if aoi_path is not None:
        aoi_geometry, aoi_area_m2 = _load_aoi_equal_area(aoi_path)
        denominator = "aoi"

    grid_area_m2, comparable_area_m2, class_areas = _cell_area_sums(
        comparison.grid,
        comparable_mask,
        change_map,
        aoi_geometry,
    )
    denominator_area_m2 = (
        aoi_area_m2 if aoi_area_m2 is not None else grid_area_m2
    )
    coverage_percent = (
        comparable_area_m2 / denominator_area_m2 * 100
        if denominator_area_m2 > 0
        else 0.0
    )
    coverage_percent = float(np.clip(coverage_percent, 0.0, 100.0))

    class_pixel_counts = {
        class_name: int(np.count_nonzero(change_map == class_value))
        for class_name, class_value in (
            ("decrease", ChangeClass.DECREASE),
            ("stable", ChangeClass.STABLE),
            ("increase", ChangeClass.INCREASE),
        )
    }
    areas_m2 = {name: float(value) for name, value in class_areas.items()}
    areas_ha = {name: value / 10_000 for name, value in areas_m2.items()}

    return NDVIChangeAnalysis(
        change_class_map=change_map,
        delta_ndvi=delta.astype(np.float32),
        comparable_mask=comparable_mask,
        statistics=_summarize_delta(delta, comparable_mask),
        areas_m2=areas_m2,
        areas_ha=areas_ha,
        class_pixel_counts=class_pixel_counts,
        comparable_area_m2=float(comparable_area_m2),
        denominator_area_m2=float(denominator_area_m2),
        comparable_coverage_percent=coverage_percent,
        coverage_denominator=denominator,
        threshold=threshold,
        excluded_out_of_range_pixels=excluded_out_of_range,
    )
