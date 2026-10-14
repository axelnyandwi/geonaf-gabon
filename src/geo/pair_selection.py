"""Select Sentinel-2 date pairs using observed common SCL coverage.

STAC search completeness is a precondition. Cheap metadata/quality filters and
explicit candidate limits run before SCL masks are aligned. Common coverage is
then measured from the pixelwise mask intersection, weighted by exact AOI cell
intersections in the equal-area CRS used by change analysis.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from itertools import combinations
from math import isfinite
from collections import OrderedDict
from pathlib import Path
from typing import Mapping, Sequence

import geopandas as gpd
import numpy as np
import rasterio
from affine import Affine
from rasterio.warp import transform_bounds
from shapely import bounds as geometry_bounds
from shapely import transform as transform_geometry

from src.diagnostics import safe_call
from src.data.cloud_mask import SCL_MASK_POLICIES
from src.data.ingestion import STACSearchResults
from src.geo.change_analysis import (
    load_equal_area_aoi,
    measure_mask_coverage,
    prepare_pixel_area_weights,
)
from src.geo.temporal import (
    RasterGrid,
    build_common_grid,
    scl_policy_mask_on_grid,
)


_PAIR_CACHE_MAX_BYTES = 128 * 1024 * 1024


def _grid_cache_key(grid: RasterGrid) -> tuple:
    return (grid.crs.to_string(), tuple(grid.transform), grid.width, grid.height)


def _bounded_cache_put(cache: OrderedDict, key: tuple, value: np.ndarray) -> None:
    """Insert into a byte-bounded LRU cache; do not retain oversized arrays."""
    size = value.nbytes
    if size > _PAIR_CACHE_MAX_BYTES:
        return
    while cache and sum(array.nbytes for array in cache.values()) + size > _PAIR_CACHE_MAX_BYTES:
        cache.popitem(last=False)
    cache[key] = value


class PairSuitability(str, Enum):
    """Operational coverage labels; they are not scientific validation."""

    INSUFFICIENT = "insufficient"
    EXPLORATORY = "exploratory"
    POTENTIALLY_REPRESENTATIVE = "potentially_representative"


class PairSearchScope(str, Enum):
    """Origin/completeness scope of the acquisition candidates."""

    STAC_COMPLETE = "stac_complete"
    LOCAL_SUBSET = "local_subset"


@dataclass(frozen=True)
class PairSelectionConfig:
    """Configurable filters and explicit work limits for pair selection.

    The default 5% exploratory and 50% potentially-representative coverage
    bands are workflow labels only. They are not universal scientific
    thresholds and should be configured for each AOI and study design.
    """

    min_interval_days: float = 1.0
    max_interval_days: float = 180.0
    min_usable_land_percent: float = 0.0
    min_aoi_coverage_percent: float = 0.0
    min_aoi_data_coverage_percent: float = 0.0
    max_cloud_cover_percent: float | None = None
    min_common_coverage_percent: float = 0.0
    exploratory_coverage_percent: float = 5.0
    representative_coverage_percent: float = 50.0
    scl_policy: str = "usable_land"
    max_acquisitions: int = 40
    max_pair_evaluations: int = 300

    def __post_init__(self) -> None:
        positive_interval = (self.min_interval_days, self.max_interval_days)
        if any(
            isinstance(value, bool) or not isinstance(value, (int, float))
            for value in positive_interval
        ):
            raise ValueError("Les bornes d'intervalle doivent être numériques.")
        if not all(isfinite(value) for value in positive_interval):
            raise ValueError("Les bornes d'intervalle doivent être finies.")
        if self.min_interval_days < 0 or self.max_interval_days < self.min_interval_days:
            raise ValueError("L'intervalle temporel configuré est invalide.")

        for name in (
            "min_usable_land_percent",
            "min_aoi_coverage_percent",
            "min_aoi_data_coverage_percent",
            "min_common_coverage_percent",
            "exploratory_coverage_percent",
            "representative_coverage_percent",
        ):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not isfinite(value)
                or not 0 <= value <= 100
            ):
                raise ValueError(f"{name} doit être compris entre 0 et 100.")

        if self.exploratory_coverage_percent > self.representative_coverage_percent:
            raise ValueError("Le seuil exploratoire dépasse le seuil représentatif.")
        if self.max_cloud_cover_percent is not None and (
            isinstance(self.max_cloud_cover_percent, bool)
            or not isinstance(self.max_cloud_cover_percent, (int, float))
            or not isfinite(self.max_cloud_cover_percent)
            or not 0 <= self.max_cloud_cover_percent <= 100
        ):
            raise ValueError("max_cloud_cover_percent doit être compris entre 0 et 100.")
        for name in ("max_acquisitions", "max_pair_evaluations"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} doit être un entier strictement positif.")
        if self.scl_policy not in SCL_MASK_POLICIES:
            raise ValueError("Politique SCL inconnue.")


@dataclass(frozen=True)
class TemporalPair:
    """One measured pair, ordered from earlier to later acquisition."""

    earlier_product_id: str
    later_product_id: str
    earlier_datetime: datetime
    later_datetime: datetime
    interval_days: float
    common_usable_pixels: int
    common_area_m2: float
    common_coverage_percent: float
    min_usable_land_percent: float
    min_aoi_coverage_percent: float
    min_aoi_data_coverage_percent: float
    cloud_cover_sum_percent: float | None
    suitability: PairSuitability


@dataclass(frozen=True)
class PairSelectionResult:
    """Ranked pairs and completeness/cost metadata for honest reporting."""

    pairs: tuple[TemporalPair, ...]
    eligible_acquisitions: int
    evaluated_pairs: int
    rejected_counts: Mapping[str, int]
    raster_failures: tuple[tuple[str, str], ...]
    acquisition_limit_reached: bool
    pair_limit_reached: bool
    search_scope: PairSearchScope = PairSearchScope.STAC_COMPLETE

    @property
    def ranking_is_complete(self) -> bool:
        """Whether all eligible acquisition pairs were raster-evaluated."""
        return (
            not self.acquisition_limit_reached
            and not self.pair_limit_reached
            and not self.raster_failures
        )


class IncompleteSTACSearchError(ValueError):
    """Raised when a truncated or unverified STAC result is supplied."""


@dataclass(frozen=True)
class _Acquisition:
    product_id: str
    acquired_at: datetime
    scl_path: Path
    usable_land_percent: float
    aoi_coverage_percent: float
    aoi_data_coverage_percent: float
    cloud_cover_percent: float | None


def _crop_grid_to_aoi_bounds(grid, aoi_frame):
    """Return the conservative pixel window enclosing the transformed AOI.

    AOI coordinates are first transformed to the grid CRS, then mapped to
    pixel coordinates with the inverse full affine. A densified transformed
    bounds envelope is also included when CRSs differ, preventing curved CRS
    edges between AOI vertices from trimming a potentially intersecting cell.
    """
    if aoi_frame.crs == grid.crs:
        aoi_in_grid_crs = aoi_frame
        projected_bounds = None
    else:
        aoi_in_grid_crs = aoi_frame.to_crs(grid.crs)
        projected_bounds = transform_bounds(
            aoi_frame.crs,
            grid.crs,
            *aoi_frame.total_bounds,
            densify_pts=21,
        )

    inverse = ~grid.transform

    def world_to_pixel(x, y):
        return (
            inverse.a * x + inverse.b * y + inverse.c,
            inverse.d * x + inverse.e * y + inverse.f,
        )

    pixel_geometries = transform_geometry(
        aoi_in_grid_crs.geometry.array,
        world_to_pixel,
        interleaved=False,
    )
    pixel_bounds = geometry_bounds(pixel_geometries)
    min_col = float(np.nanmin(pixel_bounds[:, 0]))
    min_row = float(np.nanmin(pixel_bounds[:, 1]))
    max_col = float(np.nanmax(pixel_bounds[:, 2]))
    max_row = float(np.nanmax(pixel_bounds[:, 3]))

    if projected_bounds is not None:
        left, bottom, right, top = projected_bounds
        bound_x = np.array([left, left, right, right], dtype=np.float64)
        bound_y = np.array([bottom, top, bottom, top], dtype=np.float64)
        bound_cols, bound_rows = world_to_pixel(bound_x, bound_y)
        min_col = min(min_col, float(np.min(bound_cols)))
        min_row = min(min_row, float(np.min(bound_rows)))
        max_col = max(max_col, float(np.max(bound_cols)))
        max_row = max(max_row, float(np.max(bound_rows)))

    # The pixel-space rectangle is the raster footprint for any nonsingular
    # affine, including rotated and sheared grids.
    if (
        max_col <= 0
        or max_row <= 0
        or min_col >= grid.width
        or min_row >= grid.height
    ):
        return None

    # Tiny outward padding protects rotated-grid bounds from floating-point
    # roundoff at exact pixel edges. Keep the historical north-up window exact.
    is_north_up = np.isclose(grid.transform.b, 0.0, atol=1e-12) and np.isclose(
        grid.transform.d, 0.0, atol=1e-12
    )
    tolerance = 0.0 if is_north_up else 1e-9
    col_start = max(0, int(np.floor(min_col - tolerance)))
    col_stop = min(grid.width, int(np.ceil(max_col + tolerance)))
    row_start = max(0, int(np.floor(min_row - tolerance)))
    row_stop = min(grid.height, int(np.ceil(max_row + tolerance)))
    width = col_stop - col_start
    height = row_stop - row_start
    if width <= 0 or height <= 0:
        return None

    return RasterGrid(
        crs=grid.crs,
        resolution=grid.resolution,
        transform=grid.transform @ Affine.translation(col_start, row_start),
        width=width,
        height=height,
    )


def _parse_datetime(value: object) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError("date d'acquisition absente")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("date d'acquisition invalide") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _percent(metrics: Mapping[str, object], name: str) -> float:
    value = metrics.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"métrique {name} absente")
    value = float(value)
    if not isfinite(value) or not 0 <= value <= 100:
        raise ValueError(f"métrique {name} invalide")
    return value


def _completeness_guard(
    search_results: Sequence[dict],
    search_complete: bool | None,
    search_scope: PairSearchScope,
) -> None:
    if isinstance(search_results, STACSearchResults):
        if search_results.truncated:
            raise IncompleteSTACSearchError(
                "Recherche STAC tronquée : la sélection de paires est refusée."
            )
        if search_scope is PairSearchScope.LOCAL_SUBSET:
            raise IncompleteSTACSearchError(
                "Le mode local_subset accepte uniquement une liste locale simple."
            )
        if search_complete is False:
            raise IncompleteSTACSearchError(
                "La recherche STAC est déclarée incomplète."
            )
        return
    if search_scope is PairSearchScope.LOCAL_SUBSET:
        if search_complete is True:
            raise ValueError(
                "Ne pas déclarer search_complete=True pour un sous-ensemble local."
            )
        return
    if search_complete is not True:
        raise IncompleteSTACSearchError(
            "L'exhaustivité STAC doit être explicitement confirmée pour une liste simple."
        )


def _candidate_records(
    search_results: Sequence[dict],
    scl_paths: Mapping[str, str | Path],
    quality_by_product: Mapping[str, Mapping[str, object]],
    config: PairSelectionConfig,
    rejected: dict[str, int],
) -> list[_Acquisition]:
    records: dict[str, _Acquisition] = {}
    seen_ids: set[str] = set()

    for item in search_results:
        product_id = item.get("id") if isinstance(item, dict) else None
        if not isinstance(product_id, str) or not product_id:
            rejected["invalid_item"] += 1
            continue
        if product_id in seen_ids:
            rejected["duplicate_product"] += 1
            continue
        seen_ids.add(product_id)

        properties = item.get("properties")
        if not isinstance(properties, dict):
            rejected["incomplete_metadata"] += 1
            continue
        try:
            acquired_at = _parse_datetime(properties.get("datetime"))
        except ValueError:
            rejected["incomplete_metadata"] += 1
            continue

        cloud = properties.get("eo:cloud_cover")
        if cloud is not None:
            if isinstance(cloud, bool) or not isinstance(cloud, (int, float)):
                rejected["incomplete_metadata"] += 1
                continue
            cloud = float(cloud)
            if not isfinite(cloud) or not 0 <= cloud <= 100:
                rejected["incomplete_metadata"] += 1
                continue
        if (
            config.max_cloud_cover_percent is not None
            and (cloud is None or cloud > config.max_cloud_cover_percent)
        ):
            rejected["cloud_filter"] += 1
            continue

        scl_path = scl_paths.get(product_id)
        metrics = quality_by_product.get(product_id)
        if scl_path is None or not Path(scl_path).is_file():
            rejected["incomplete_local_data"] += 1
            continue
        if not isinstance(metrics, Mapping):
            rejected["incomplete_quality"] += 1
            continue
        try:
            usable = _percent(metrics, "usable_land_percent")
            aoi_coverage = _percent(metrics, "aoi_coverage_percent")
            aoi_data_coverage = _percent(metrics, "aoi_data_coverage_percent")
        except ValueError:
            rejected["incomplete_quality"] += 1
            continue
        if (
            usable < config.min_usable_land_percent
            or aoi_coverage < config.min_aoi_coverage_percent
            or aoi_data_coverage < config.min_aoi_data_coverage_percent
        ):
            rejected["quality_filter"] += 1
            continue

        records[product_id] = _Acquisition(
            product_id=product_id,
            acquired_at=acquired_at,
            scl_path=Path(scl_path),
            usable_land_percent=usable,
            aoi_coverage_percent=aoi_coverage,
            aoi_data_coverage_percent=aoi_data_coverage,
            cloud_cover_percent=cloud,
        )

    return list(records.values())


def _suitability(coverage: float, config: PairSelectionConfig) -> PairSuitability:
    if coverage < config.exploratory_coverage_percent:
        return PairSuitability.INSUFFICIENT
    if coverage < config.representative_coverage_percent:
        return PairSuitability.EXPLORATORY
    return PairSuitability.POTENTIALLY_REPRESENTATIVE


def select_temporal_pairs(
    search_results: Sequence[dict],
    *,
    scl_paths: Mapping[str, str | Path],
    quality_by_product: Mapping[str, Mapping[str, object]],
    aoi_path: str | Path,
    config: PairSelectionConfig | None = None,
    search_complete: bool | None = None,
    search_scope: PairSearchScope | str = PairSearchScope.STAC_COMPLETE,
) -> PairSelectionResult:
    """Filter, measure, and rank date pairs from a completed STAC search.

    ``quality_by_product`` must contain AOI-scoped outputs from
    :func:`src.data.quality.calculate_scl_quality`; scene-wide cloud metrics
    are not substituted for local SCL quality. Plain lists require
    ``search_complete=True`` unless ``search_scope="local_subset"`` is
    explicitly selected for a local diagnostic. That mode accepts only a
    plain list, never claims historical/STAC exhaustiveness, and rejects a
    contradictory ``search_complete=True``. Results from
    :func:`search_sentinel2` carry their own truncation state and are rejected
    when truncated in every mode.

    Cheap date, cloud, quality, and local-file checks run before raster mask
    alignment. Pair evaluation is capped. When a cap is reached, the result
    says its ranking is incomplete; measured coverage is never inferred from
    per-acquisition percentages. Final ties break by minimum usable-land,
    minimum AOI data/geographic coverage, lower combined STAC cloud cover,
    shorter interval, then product IDs. Under the pair cap, closest dates are
    evaluated first, then higher minimum usable-land/data coverage, then IDs.
    """
    config = config or PairSelectionConfig()
    try:
        scope = PairSearchScope(search_scope)
    except ValueError:
        raise ValueError("search_scope doit être 'stac_complete' ou 'local_subset'.") from None
    _completeness_guard(search_results, search_complete, scope)
    rejected = {
        name: 0
        for name in (
            "invalid_item",
            "duplicate_product",
            "incomplete_metadata",
            "cloud_filter",
            "incomplete_local_data",
            "incomplete_quality",
            "quality_filter",
            "date_filter",
            "no_raster_intersection",
            "common_coverage_filter",
            "raster_error",
        )
    }
    raster_failures: list[tuple[str, str]] = []

    # Read/validate the AOI once. Pair work uses its bounds to avoid warping
    # full Sentinel tiles, then exact equal-area intersections for coverage.
    aoi_frame = safe_call(gpd.read_file, aoi_path, paths=(aoi_path,))
    if aoi_frame.empty or aoi_frame.crs is None:
        raise ValueError("AOI vide ou CRS manquant.")
    aoi_area = load_equal_area_aoi(aoi_path)

    acquisitions = _candidate_records(
        search_results,
        scl_paths,
        quality_by_product,
        config,
        rejected,
    )
    eligible_acquisition_count = len(acquisitions)
    if eligible_acquisition_count > config.max_acquisitions:
        acquisition_limit_reached = True
        acquisitions = sorted(
            acquisitions,
            key=lambda candidate: (
                -candidate.usable_land_percent,
                -candidate.aoi_data_coverage_percent,
                -candidate.aoi_coverage_percent,
                candidate.cloud_cover_percent if candidate.cloud_cover_percent is not None else float("inf"),
                candidate.acquired_at,
                candidate.product_id,
            ),
        )[: config.max_acquisitions]
    else:
        acquisition_limit_reached = False
    acquisitions.sort(key=lambda candidate: (candidate.acquired_at, candidate.product_id))

    possible_pairs = []
    for earlier, later in combinations(acquisitions, 2):
        interval_days = (later.acquired_at - earlier.acquired_at).total_seconds() / 86400
        if not config.min_interval_days <= interval_days <= config.max_interval_days:
            rejected["date_filter"] += 1
            continue
        possible_pairs.append((earlier, later, interval_days))

    possible_pairs.sort(
        key=lambda pair: (
            pair[2],
            -min(pair[0].usable_land_percent, pair[1].usable_land_percent),
            -min(pair[0].aoi_data_coverage_percent, pair[1].aoi_data_coverage_percent),
            pair[0].product_id,
            pair[1].product_id,
        )
    )
    pair_limit_reached = len(possible_pairs) > config.max_pair_evaluations
    pairs_to_evaluate = possible_pairs[: config.max_pair_evaluations]

    measured_pairs = []
    aligned_mask_cache: OrderedDict[tuple, np.ndarray] = OrderedDict()
    area_weight_cache: OrderedDict[tuple, np.ndarray] = OrderedDict()
    for earlier, later, interval_days in pairs_to_evaluate:
        try:
            full_grid = build_common_grid(earlier.scl_path, later.scl_path)
        except ValueError as error:
            if "aucune intersection" in str(error).lower() or "trop petite" in str(error).lower():
                rejected["no_raster_intersection"] += 1
                continue
            raise
        grid = _crop_grid_to_aoi_bounds(full_grid, aoi_frame)
        if grid is None:
            rejected["no_raster_intersection"] += 1
            continue
        try:
            grid_key = _grid_cache_key(grid)

            def aligned_mask(acquisition: _Acquisition) -> np.ndarray:
                key = (acquisition.product_id, grid_key, config.scl_policy)
                cached = aligned_mask_cache.get(key)
                if cached is not None:
                    aligned_mask_cache.move_to_end(key)
                    return cached
                result = scl_policy_mask_on_grid(
                    acquisition.scl_path, grid, config.scl_policy
                )
                result.setflags(write=False)
                _bounded_cache_put(aligned_mask_cache, key, result)
                return result

            mask_earlier = aligned_mask(earlier)
            mask_later = aligned_mask(later)
            common_mask = mask_earlier & mask_later
            weights = area_weight_cache.get(grid_key)
            if weights is None:
                weights = prepare_pixel_area_weights(grid, aoi_area)
                weights.setflags(write=False)
                _bounded_cache_put(area_weight_cache, grid_key, weights)
            else:
                area_weight_cache.move_to_end(grid_key)
            coverage = measure_mask_coverage(
                common_mask,
                grid,
                aoi_area,
                pixel_area_weights=weights,
            )
        except (OSError, rasterio.errors.RasterioError) as error:
            # Keep diagnostics actionable without returning local paths or raw driver messages.
            rejected["raster_error"] += 1
            raster_failures.append(
                (
                    f"{earlier.product_id}|{later.product_id}",
                    type(error).__name__,
                )
            )
            continue

        if coverage.coverage_percent < config.min_common_coverage_percent:
            rejected["common_coverage_filter"] += 1
            continue
        clouds = (earlier.cloud_cover_percent, later.cloud_cover_percent)
        measured_pairs.append(
            TemporalPair(
                earlier_product_id=earlier.product_id,
                later_product_id=later.product_id,
                earlier_datetime=earlier.acquired_at,
                later_datetime=later.acquired_at,
                interval_days=interval_days,
                common_usable_pixels=coverage.pixel_count,
                common_area_m2=coverage.area_m2,
                common_coverage_percent=coverage.coverage_percent,
                min_usable_land_percent=min(
                    earlier.usable_land_percent, later.usable_land_percent
                ),
                min_aoi_coverage_percent=min(
                    earlier.aoi_coverage_percent, later.aoi_coverage_percent
                ),
                min_aoi_data_coverage_percent=min(
                    earlier.aoi_data_coverage_percent,
                    later.aoi_data_coverage_percent,
                ),
                cloud_cover_sum_percent=(
                    earlier.cloud_cover_percent + later.cloud_cover_percent
                    if all(value is not None for value in clouds)
                    else None
                ),
                suitability=_suitability(coverage.coverage_percent, config),
            )
        )

    measured_pairs.sort(
        key=lambda pair: (
            -pair.common_coverage_percent,
            -pair.min_usable_land_percent,
            -pair.min_aoi_data_coverage_percent,
            -pair.min_aoi_coverage_percent,
            pair.cloud_cover_sum_percent if pair.cloud_cover_sum_percent is not None else float("inf"),
            pair.interval_days,
            pair.earlier_product_id,
            pair.later_product_id,
        )
    )

    return PairSelectionResult(
        pairs=tuple(measured_pairs),
        eligible_acquisitions=eligible_acquisition_count,
        evaluated_pairs=len(pairs_to_evaluate),
        rejected_counts=rejected,
        raster_failures=tuple(raster_failures),
        acquisition_limit_reached=acquisition_limit_reached,
        pair_limit_reached=pair_limit_reached,
        search_scope=scope,
    )
