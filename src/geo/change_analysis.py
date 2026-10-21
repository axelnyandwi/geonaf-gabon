"""Change classes and area statistics for a harmonized NDVI date pair."""

from __future__ import annotations

from dataclasses import dataclass
from collections import OrderedDict
from enum import IntEnum
from hashlib import sha256
from pathlib import Path
from threading import RLock

import numpy as np
from pyproj import Transformer
from rasterio.features import rasterize
from shapely import (
    area as geometry_area,
    covers,
    disjoint,
    intersection,
    is_empty,
    is_valid,
    polygons,
    prepare as prepare_geometry,
    relate_pattern,
    union_all,
)
from shapely.geometry import box, mapping

from src.diagnostics import safe_call
from src.geo.temporal import RasterGrid, TemporalComparison


EQUAL_AREA_CRS = "EPSG:6933"
DEFAULT_CHANGE_THRESHOLD = 0.10
MAX_AREA_CELLS_PER_CHUNK = 100_000
AREA_BLOCK_SIZE = 256
AREA_CACHE_MAX_BYTES = 64 * 1024 * 1024
AREA_CACHE_MAX_ENTRIES = 4
_AREA_ALGORITHM_VERSION = "projected-quadrilateral-v1"
_area_cache = OrderedDict()
_area_cache_lock = RLock()
_area_cache_hits = 0
_area_cache_misses = 0


def clear_area_weight_cache():
    """Clear process-local weights and diagnostic counters atomically."""
    global _area_cache_hits, _area_cache_misses
    with _area_cache_lock:
        _area_cache.clear()
        _area_cache_hits = _area_cache_misses = 0


def area_weight_cache_info():
    """Return aggregate diagnostics, never geometry, keys or source paths."""
    with _area_cache_lock:
        return {"entries": len(_area_cache),
                "bytes": sum(v.nbytes for v in _area_cache.values()),
                "hits": _area_cache_hits, "misses": _area_cache_misses}


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
    common grid only, not geographic coverage of an AOI. With an AOI,
    descriptive statistics and class pixel counts use comparable cells selected
    by ``all_touched``; surface indicators retain exact AOI/cell intersections.
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
    cached = _cached_area_weights(grid, aoi.geometry)
    if cached is not None:
        # Public callers own their array, including the right to mutate it.
        return cached.copy()
    return _compute_area_weights(grid, aoi.geometry)


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


def _rasterize_aoi_pixel_mask(grid: RasterGrid, aoi_path: str | Path) -> np.ndarray:
    """Rasterize AOI membership using the export's all-touched pixel rule."""
    import geopandas as gpd

    aoi = safe_call(gpd.read_file, aoi_path, paths=(aoi_path,))
    if aoi.empty or aoi.crs is None:
        raise ValueError("L'AOI doit être non vide et avoir un CRS.")
    if any(geometry is None or is_empty(geometry) or not is_valid(geometry) for geometry in aoi.geometry):
        raise ValueError("L'AOI contient une géométrie vide ou invalide.")
    shapes = [(mapping(geometry), 1) for geometry in aoi.to_crs(grid.crs).geometry]
    return rasterize(
        shapes,
        out_shape=(grid.height, grid.width),
        transform=grid.transform,
        fill=0,
        all_touched=True,
        dtype="uint8",
    ).astype(bool)


def load_equal_area_aoi(aoi_path: str | Path) -> EqualAreaAOI:
    """Load and validate an AOI once for repeated raster coverage measures."""
    geometry, area_m2 = _load_aoi_equal_area(aoi_path)
    return EqualAreaAOI(geometry=geometry, area_m2=area_m2)


def _area_cache_key(grid, geometry):
    # Hash the actual projected geometry: edits with unchanged file metadata
    # still invalidate reuse. No coordinates or file paths remain in the key.
    geometry_key = None if geometry is None else sha256(geometry.wkb).digest()
    return (_AREA_ALGORITHM_VERSION, grid.crs.to_wkt(), EQUAL_AREA_CRS,
            tuple(grid.transform), grid.width, grid.height, tuple(grid.resolution),
            geometry_key, AREA_BLOCK_SIZE, MAX_AREA_CELLS_PER_CHUNK)


def _cached_area_weights(grid, geometry):
    global _area_cache_hits, _area_cache_misses
    size = grid.width * grid.height * np.dtype(np.float64).itemsize
    # Oversized analyses stream instead of allocating a full weight matrix.
    if size > AREA_CACHE_MAX_BYTES or AREA_CACHE_MAX_ENTRIES < 1:
        return None
    key = _area_cache_key(grid, geometry)
    with _area_cache_lock:
        if key in _area_cache:
            _area_cache_hits += 1
            _area_cache.move_to_end(key)
            return _area_cache[key]
        _area_cache_misses += 1
        # A single builder bounds concurrent scratch allocations. Holding the
        # lock also prevents publishing stale work across an explicit clear.
        # Evict before allocation, not after exceeding the storage budget.
        while _area_cache and (
            len(_area_cache) >= AREA_CACHE_MAX_ENTRIES
            or sum(v.nbytes for v in _area_cache.values()) + size > AREA_CACHE_MAX_BYTES
        ):
            _area_cache.popitem(last=False)
        weights = _compute_area_weights(grid, geometry)
        weights.flags.writeable = False
        _area_cache[key] = weights
        return weights


def _projected_tile(grid, transformer, row, end_row, col, end_col):
    # Shared vertices retain the exact six-term affine expression used by the
    # oracle. PROJ transforms each distinct vertex once, rather than five times.
    rows, cols = np.meshgrid(np.arange(row, end_row + 1, dtype=np.float64),
                             np.arange(col, end_col + 1, dtype=np.float64),
                             indexing="ij")
    affine = grid.transform
    x = affine.a * cols + affine.b * rows + affine.c
    y = affine.d * cols + affine.e * rows + affine.f
    x, y = transformer.transform(x, y)
    if not np.all(np.isfinite(x)) or not np.all(np.isfinite(y)):
        raise ValueError("La projection des cellules produit des coordonnées non finies.")
    return np.stack((x, y), axis=-1)


def _quadrilateral_areas(vertices):
    # GEOS Area::ofRingSigned arithmetic, with the first x as origin, avoids
    # cancellation of large absolute coordinates. Preserve its operation order.
    p0 = vertices[:-1, :-1]
    p1 = vertices[:-1, 1:]
    p2 = vertices[1:, 1:]
    p3 = vertices[1:, :-1]
    total = (p1[..., 0] - p0[..., 0]) * (p0[..., 1] - p2[..., 1])
    total += (p2[..., 0] - p0[..., 0]) * (p1[..., 1] - p3[..., 1])
    total += (p3[..., 0] - p0[..., 0]) * (p2[..., 1] - p0[..., 1])
    return np.abs(total / 2.0)


def _fill_projected_block(vertices, geometry, output):
    # The bounds include ALL transformed vertices, not just four tile corners.
    # Every straight-edged quadrilateral of the oracle lies within this box,
    # even for nonlinear projections. AOI predicates run in its EPSG:6933 CRS.
    envelope = box(float(vertices[..., 0].min()), float(vertices[..., 1].min()),
                   float(vertices[..., 0].max()), float(vertices[..., 1].max()))
    if geometry is None or covers(geometry, envelope):
        output[:] = _quadrilateral_areas(vertices)
        return
    if disjoint(geometry, envelope):
        output[:] = 0.0
        return
    height, width = output.shape
    if height * width > 64:
        # Subdivide ambiguous boxes; holes and concavities cannot be classified
        # as interior merely because their outer corners lie in the AOI.
        if height >= width:
            middle = height // 2
            _fill_projected_block(vertices[:middle + 1], geometry, output[:middle])
            _fill_projected_block(vertices[middle:], geometry, output[middle:])
        else:
            middle = width // 2
            _fill_projected_block(vertices[:, :middle + 1], geometry, output[:, :middle])
            _fill_projected_block(vertices[:, middle:], geometry, output[:, middle:])
        return
    coordinates = np.stack((vertices[:-1, :-1], vertices[:-1, 1:],
                            vertices[1:, 1:], vertices[1:, :-1],
                            vertices[:-1, :-1]), axis=2).reshape(-1, 5, 2)
    cells = polygons(coordinates)
    values = np.zeros(cells.size, dtype=np.float64)
    interior = np.asarray(covers(geometry, cells), dtype=bool)
    values[interior] = geometry_area(cells[interior])
    partial = np.flatnonzero(~interior)
    if partial.size:
        overlapping = np.asarray(relate_pattern(geometry, cells[partial], "T********"),
                                 dtype=bool)
        partial = partial[overlapping]
        if partial.size:
            values[partial] = geometry_area(intersection(cells[partial], geometry))
    output[:] = values.reshape(output.shape)


def _tile_size():
    # Bound both axes, including rasters wider than the former row-strip cap.
    return max(1, min(AREA_BLOCK_SIZE, int(np.sqrt(MAX_AREA_CELLS_PER_CHUNK))))


def _compute_area_weights(grid, geometry):
    weights = np.empty((grid.height, grid.width), dtype=np.float64)
    transformer = Transformer.from_crs(grid.crs, EQUAL_AREA_CRS, always_xy=True)
    if geometry is not None:
        prepare_geometry(geometry)
    side = _tile_size()
    for row in range(0, grid.height, side):
        end_row = min(row + side, grid.height)
        for col in range(0, grid.width, side):
            end_col = min(col + side, grid.width)
            vertices = _projected_tile(grid, transformer, row, end_row, col, end_col)
            _fill_projected_block(vertices, geometry, weights[row:end_row, col:end_col])
    return weights


def _reduction_slices(grid):
    # Preserve the old summation strips whenever their allocation is bounded.
    # Exception: a row wider than the ceiling must also be split horizontally.
    rows = max(1, min(128, MAX_AREA_CELLS_PER_CHUNK // grid.width))
    columns = grid.width if grid.width <= MAX_AREA_CELLS_PER_CHUNK else _tile_size()
    for row in range(0, grid.height, rows):
        for col in range(0, grid.width, columns):
            yield (slice(row, min(row + rows, grid.height)),
                   slice(col, min(col + columns, grid.width)))


def _pixel_area_chunks(grid: RasterGrid, aoi_geometry=None):
    """Stream bounded weights with geometry scratch limited to 256x256 cells.

    Reductions keep the former row ordering unless even one row exceeds the
    cell ceiling; that case uses bounded column segments as well.
    """
    transformer = Transformer.from_crs(grid.crs, EQUAL_AREA_CRS, always_xy=True)
    if aoi_geometry is not None:
        prepare_geometry(aoi_geometry)
    side = _tile_size()
    for selection in _reduction_slices(grid):
        rows, cols = selection
        values = np.empty((rows.stop - rows.start, cols.stop - cols.start), dtype=np.float64)
        for row in range(rows.start, rows.stop, side):
            end_row = min(row + side, rows.stop)
            for col in range(cols.start, cols.stop, side):
                end_col = min(col + side, cols.stop)
                vertices = _projected_tile(grid, transformer, row, end_row, col, end_col)
                _fill_projected_block(vertices, aoi_geometry,
                                      values[row-rows.start:end_row-rows.start,
                                             col-cols.start:end_col-cols.start])
        yield selection, values


def _area_weight_chunks(grid, geometry):
    weights = _cached_area_weights(grid, geometry)
    if weights is None:
        yield from _pixel_area_chunks(grid, geometry)
        return
    for selection in _reduction_slices(grid):
        yield selection, weights[selection]


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

    for selection, pixel_areas in _area_weight_chunks(grid, aoi_geometry):
        total_grid_area += float(pixel_areas.sum())
        comparable_chunk = comparable_mask[selection]
        class_chunk = class_map[selection]
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
        for selection in _reduction_slices(grid):
            weight_chunk = weights[selection]
            mask_chunk = mask[selection]
            covered_area_m2 += float(weight_chunk[mask_chunk].sum(dtype=np.float64))
            covered_pixel_count += int(
                np.count_nonzero(mask_chunk & (weight_chunk > 0))
            )
    else:
        for selection, pixel_areas in _area_weight_chunks(grid, aoi_geometry):
            mask_chunk = mask[selection]
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
    With an AOI, descriptive statistics and class counts include comparable
    cells touched by the AOI, matching the exported raster pixel rule. A
    partial cell counts once in these pixel indicators; its surfaces are still
    weighted by its exact polygon intersection with the unioned AOI. Without
    an AOI, statistics/counts retain the full comparable grid and coverage is
    relative to all cells in the common grid, labelled ``comparison_grid``.
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
    excluded_mask = source_comparable.copy()
    aoi_pixel_mask = None
    if aoi_path is not None:
        aoi_pixel_mask = _rasterize_aoi_pixel_mask(comparison.grid, aoi_path)
        excluded_mask &= aoi_pixel_mask
    excluded_out_of_range = int(
        np.count_nonzero(excluded_mask & np.isfinite(date_1) & np.isfinite(date_2) & ~in_ndvi_range)
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

    indicator_mask = comparable_mask
    if aoi_pixel_mask is not None:
        indicator_mask = comparable_mask & aoi_pixel_mask

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
        class_name: int(np.count_nonzero(indicator_mask & (change_map == class_value)))
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
        statistics=_summarize_delta(delta, indicator_mask),
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
