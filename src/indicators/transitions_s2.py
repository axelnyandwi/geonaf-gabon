"""Two Sentinel-2 acquisitions on their strictly common observed surface: pure computation on arrays.

No file is read here. The NDVI of both dates, their validity masks and the
common mask come from the existing pair chain
(``analyze_temporal_vegetation_change``: ``comparison`` and
``analysis.comparable_mask``); exact pixel/zone area weights come from
``prepare_pixel_area_weights`` (EPSG:6933, partially covered pixels count for
their part inside the zone). Nothing is resampled here.

On the common set ``C`` (comparable and inside the zone):

* surfaces: ``A_C`` and, per date, the valid surface on the common grid;
* area-weighted NDVI mean and lower weighted median per date, and of ΔNDVI;
* **spectral class transitions** around the NDVI class threshold ``t``
  (float32 comparison, equality counted as ≥ t): ≥t→<t, <t→≥t, stayed ≥t,
  stayed <t, which add up to ``A_C``; the net balance ``A(<t→≥t) − A(≥t→<t)``
  is a difference of transition surfaces, not a gain or loss of vegetation;
* a spatial diagnostic by 1 km² blocks of a fixed equal-area grid (EPSG:6933,
  origin 0, 0), independent of the image grid: information, never a gate.

The class threshold ``t`` (0.4) is not the ΔNDVI variation threshold (0.10)
of the historical change classes, which are left untouched.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from src.geo.change_analysis import EqualAreaAOI, measure_mask_coverage
from src.indicators.vegetation_s2 import PARTITION_RTOL

BLOCK_ROWS = 256        # Rows of pixels projected at once for the block diagnostic (bounded memory).


@dataclass(frozen=True)
class TransitionAreas:
    threshold: float
    down_m2: float          # NDVI ≥ t at date 1, < t at date 2.
    up_m2: float            # NDVI < t at date 1, ≥ t at date 2.
    stayed_above_m2: float
    stayed_below_m2: float

    @property
    def net_m2(self) -> float:
        """Net balance of transition surfaces (up − down); not a gain or loss of vegetation."""
        return self.up_m2 - self.down_m2


@dataclass(frozen=True)
class BlockDiagnostic:
    crs: str
    size_m: float
    eligible_blocks: int            # Blocks covered by the zone for at least ``min_zone_share`` of their area.
    covered_share: float | None     # Share of eligible blocks whose common surface ≥ ``common_min_share``.
    empty_share: float | None       # Share of eligible blocks without any common surface.
    min_zone_share: float
    common_min_share: float


@dataclass(frozen=True)
class PairMeasure:
    zone_m2: float
    common_m2: float
    valid_1_m2: float
    valid_2_m2: float
    mean_1: float | None
    mean_2: float | None
    median_1: float | None
    median_2: float | None
    delta_mean: float | None
    delta_median: float | None
    transitions: TransitionAreas
    sensitivity: tuple[TransitionAreas, ...]
    blocks: BlockDiagnostic
    common_pixels: int
    resolution: tuple[float, float]
    crs: str

    @property
    def common_coverage_percent(self) -> float:
        return 100.0 * self.common_m2 / self.zone_m2


def weighted_median(values, weights) -> float | None:
    """Lower weighted median: the smallest value whose cumulative weight reaches half the total."""
    values, weights = np.asarray(values, dtype=np.float64), np.asarray(weights, dtype=np.float64)
    keep = weights > 0
    values, weights = values[keep], weights[keep]
    if values.size == 0:
        return None
    order = np.argsort(values, kind="stable")
    cumulative = np.cumsum(weights[order])
    return float(values[order][np.searchsorted(cumulative, 0.5 * cumulative[-1])])


def _check(ndvi_1, ndvi_2, masks, weights, grid):
    shape = (grid.height, grid.width)
    for ndvi in (ndvi_1, ndvi_2):
        if not isinstance(ndvi, np.ndarray) or ndvi.dtype != np.float32:
            raise TypeError("NDVI attendu en float32 aux deux dates (précision de la comparaison au seuil).")
    for array in (ndvi_1, ndvi_2, weights, *masks):
        if np.shape(array) != shape:
            raise ValueError("Tableau de dimensions différentes de la grille commune.")


def _transitions(ndvi_1, ndvi_2, common, area, threshold) -> TransitionAreas:
    t = np.float32(threshold)
    above_1, above_2 = ndvi_1 >= t, ndvi_2 >= t
    return TransitionAreas(float(threshold), area(common & above_1 & ~above_2), area(common & ~above_1 & above_2),
                           area(common & above_1 & above_2), area(common & ~above_1 & ~above_2))


def block_diagnostic(common, weights, grid, *, size_m: float, crs: str, min_zone_share: float,
                     common_min_share: float) -> BlockDiagnostic:
    """Common surface per block of a fixed metric grid (origin 0, 0 in ``crs``).

    Each pixel goes wholly to the block containing its centre (pixels are 10 m,
    blocks 1 km). Block areas are compared with exact equal-area weights, so
    ``crs`` should be equal-area (EPSG:6933).
    """
    from pyproj import Transformer

    transformer = Transformer.from_crs(grid.crs, crs, always_xy=True)
    zone_by_block, common_by_block = {}, {}
    weights = np.asarray(weights, dtype=np.float64)
    for start in range(0, grid.height, BLOCK_ROWS):
        rows, cols = np.nonzero(weights[start:start + BLOCK_ROWS] > 0)
        if rows.size == 0:
            continue
        rows = rows + start
        x, y = grid.transform * (cols + 0.5, rows + 0.5)
        bx, by = transformer.transform(x, y)
        keys = np.floor(np.asarray(bx) / size_m).astype(np.int64) * 10_000_000 + \
            np.floor(np.asarray(by) / size_m).astype(np.int64)
        unique, inverse = np.unique(keys, return_inverse=True)
        zone = np.bincount(inverse, weights=weights[rows, cols])
        shared = np.bincount(inverse, weights=weights[rows, cols] * common[rows, cols])
        for key, z, c in zip(unique.tolist(), zone, shared):
            zone_by_block[key] = zone_by_block.get(key, 0.0) + float(z)
            common_by_block[key] = common_by_block.get(key, 0.0) + float(c)
    eligible = [key for key, z in zone_by_block.items() if z >= min_zone_share * size_m * size_m]
    if not eligible:
        return BlockDiagnostic(crs, size_m, 0, None, None, min_zone_share, common_min_share)
    shares = [common_by_block[key] / zone_by_block[key] for key in eligible]
    return BlockDiagnostic(crs, size_m, len(eligible), sum(s >= common_min_share for s in shares) / len(shares),
                           sum(s == 0 for s in shares) / len(shares), min_zone_share, common_min_share)


def measure_pair(ndvi_1, ndvi_2, comparable, valid_1, valid_2, weights, grid, zone: EqualAreaAOI, *,
                 threshold: float, sensitivity=(), block_size_m: float = 1000.0, block_crs: str = "EPSG:6933",
                 block_min_zone_share: float = 0.5, block_common_min_share: float = 0.10) -> PairMeasure:
    """Statistics and class transitions on the common surface (see module docstring)."""
    masks = tuple(np.asarray(m, dtype=bool) for m in (comparable, valid_1, valid_2))
    _check(ndvi_1, ndvi_2, masks, weights, grid)
    weights = np.asarray(weights)
    # The chain's masks are used as they are (weights are 0 outside the zone): same elements, same summation
    # order as the chain, hence the very same common surface.
    common, valid_1, valid_2 = masks
    area = lambda mask: measure_mask_coverage(mask, grid, zone, pixel_area_weights=weights).area_m2
    common_m2 = area(common)
    w = weights[common].astype(np.float64)
    v1, v2 = ndvi_1[common].astype(np.float64), ndvi_2[common].astype(np.float64)
    if common_m2 > 0:
        mean_1, mean_2 = float(np.average(v1, weights=w)), float(np.average(v2, weights=w))
        stats = (mean_1, mean_2, weighted_median(v1, w), weighted_median(v2, w), mean_2 - mean_1,
                 weighted_median(v2 - v1, w))
    else:
        stats = (None,) * 6
    transitions = _transitions(ndvi_1, ndvi_2, common, area, threshold)
    total = transitions.down_m2 + transitions.up_m2 + transitions.stayed_above_m2 + transitions.stayed_below_m2
    if abs(total - common_m2) > PARTITION_RTOL * common_m2 + 1e-9:
        raise ValueError("Transitions incomplètes : leur somme diffère de la surface commune.")
    sensitivity = tuple(transitions if t == threshold else _transitions(ndvi_1, ndvi_2, common, area, t)
                        for t in sorted(set(sensitivity) | {threshold}))
    blocks = block_diagnostic(common, weights, grid, size_m=block_size_m, crs=block_crs,
                              min_zone_share=block_min_zone_share, common_min_share=block_common_min_share)
    return PairMeasure(zone.area_m2, common_m2, area(valid_1), area(valid_2), *stats, transitions=transitions,
                       sensitivity=sensitivity, blocks=blocks,
                       common_pixels=int(np.count_nonzero(common & (weights > 0))),
                       resolution=tuple(abs(float(v)) for v in grid.resolution), crs=str(grid.crs))


def measure_pair_result(result, profile, zone: EqualAreaAOI, weights=None) -> PairMeasure:
    """:func:`measure_pair` on a ``TemporalVegetationChangeResult``: its common mask, NDVI and grid as they are.

    ``weights`` defaults to ``prepare_pixel_area_weights`` on the result's grid,
    the cached weights that the chain itself sums.
    """
    from src.geo.change_analysis import prepare_pixel_area_weights

    comparison, grid = result.comparison, result.common_grid
    if weights is None:
        weights = prepare_pixel_area_weights(grid, zone)
    return measure_pair(comparison.ndvi_date_1, comparison.ndvi_date_2, result.analysis.comparable_mask,
                        comparison.valid_date_1, comparison.valid_date_2, weights, grid, zone,
                        threshold=profile.threshold, sensitivity=profile.vegetation.sensitivity_thresholds,
                        block_size_m=profile.block_size_m, block_crs=profile.block_crs,
                        block_min_zone_share=profile.block_min_zone_share,
                        block_common_min_share=profile.block_common_min_share)


def interval_days(earlier_utc, later_utc) -> float:
    return (later_utc - earlier_utc).total_seconds() / 86400.0
