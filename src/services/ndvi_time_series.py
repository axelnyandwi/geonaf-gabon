"""NDVI time series over one AOI and one scientific grid; no UI, no network.

Each date follows exactly the pair pipeline of ``temporal_analysis``: AOI clip
(all touched), BOA calibration from the product XML, NDVI, then reading on the
common grid with the profile's SCL policy (`_read_ndvi_on_grid` and
`scl_policy_mask_on_grid`). Two families of statistics are kept apart:

* per-date statistics use the pixels valid on that date only. Their pixel sets
  differ between dates, so they are NOT comparable as a trend;
* common-pixel statistics use only pixels valid on every retained date.

Memory stays bounded for long series: one date's arrays at a time, validity
masks packed to one bit per cell, and a second pass re-reading the temporary
NDVI rasters instead of keeping N float arrays. No status is ever "validated".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
from types import MappingProxyType
from typing import Mapping, Sequence

import numpy as np

from src.data.acquisition import Sentinel2Acquisition
from src.data.cloud_mask import change_policy_restrictions
from src.data.preparation import clip_raster_to_aoi
from src.geo.change_analysis import (
    _rasterize_aoi_pixel_mask, load_equal_area_aoi, measure_mask_coverage,
    prepare_pixel_area_weights,
)
from src.geo.scientific_profile import (
    InterpretationAssessment, ScientificProfile, ScientificStatus,
)
from src.geo.temporal import (
    RasterGrid, _read_ndvi_on_grid, build_series_grid, scl_policy_mask_on_grid,
)
from src.geo.vegetation_indices import calculate_ndvi
from src.services.temporal_analysis import (
    _profile_context, _revalidate, _utc, _write_temporary_ndvi,
)

MIN_SERIES_DATES = 3


@dataclass(frozen=True)
class NDVISummary:
    """Descriptive NDVI statistics over one explicit pixel set."""

    count: int
    mean: float | None
    median: float | None
    std: float | None
    p25: float | None
    p75: float | None


@dataclass(frozen=True)
class SeriesDate:
    """One acquisition of the series, with its own and common-pixel statistics."""

    product_id: str
    acquired_at_utc: datetime
    season_id: str | None
    calibration: Mapping[str, float]
    valid_coverage_percent: float
    valid_area_m2: float
    own: NDVISummary
    out_of_range_pixels: int
    included: bool
    exclusion_reason: str | None
    common: NDVISummary | None
    source_sha256: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))


@dataclass(frozen=True)
class SeriesGap:
    start_utc: datetime | None
    end_utc: datetime | None
    days: float
    kind: str  # "period_start", "between_dates", "period_end"
    exceeds_warning: bool


@dataclass(frozen=True)
class NDVITimeSeriesResult:
    dates: tuple[SeriesDate, ...]
    grid: RasterGrid
    scl_policy: str
    scientific_profile: ScientificProfile
    aoi_sha256: str
    period: tuple[date, date] | None
    common_pixel_count: int
    common_area_m2: float
    common_coverage_percent: float
    gaps: tuple[SeriesGap, ...]
    gap_warning_days: float
    interpretation: InterpretationAssessment
    computed_at_utc: datetime
    interpretation_scope: str = (
        "Évolution descriptive du NDVI ; aucune tendance validée ni cause attribuée."
    )

    @property
    def included_dates(self) -> tuple[SeriesDate, ...]:
        return tuple(item for item in self.dates if item.included)


def _summary(values: np.ndarray) -> NDVISummary:
    values = values.astype(np.float64, copy=False)
    if values.size == 0:
        return NDVISummary(0, None, None, None, None, None)
    p25, median, p75 = np.percentile(values, [25, 50, 75])
    return NDVISummary(int(values.size), float(np.mean(values)), float(median),
                       float(np.std(values, ddof=0)), float(p25), float(p75))


def _validate_inputs(acquisitions, aoi_path, profile, period, gap_warning_days):
    if not isinstance(profile, ScientificProfile):
        raise TypeError("Un ScientificProfile est requis pour une série NDVI.")
    if aoi_path is None:
        raise ValueError("Une AOI est requise pour une série NDVI.")
    if len(acquisitions) < MIN_SERIES_DATES:
        raise ValueError(f"Une série exige au moins {MIN_SERIES_DATES} acquisitions.")
    if (isinstance(gap_warning_days, bool) or not isinstance(gap_warning_days, (int, float))
            or not np.isfinite(gap_warning_days) or gap_warning_days <= 0):
        raise ValueError("Le seuil de lacune doit être un nombre de jours positif.")
    if period is not None:
        start, end = period
        if not all(isinstance(day, date) and not isinstance(day, datetime) for day in period) or start > end:
            raise ValueError("Période invalide.")
    ordered = sorted(acquisitions, key=lambda item: _utc(item.acquired_at))
    instants = [_utc(item.acquired_at) for item in ordered]
    if len(set(instants)) != len(instants) or len({item.product_id for item in ordered}) != len(ordered):
        raise ValueError("Les acquisitions de la série doivent être distinctes.")
    if period is not None and any(not period[0] <= instant.date() <= period[1] for instant in instants):
        raise ValueError("Acquisition hors de la période demandée.")
    return ordered


def _gaps(included, period, warning_days):
    instants = [item.acquired_at_utc for item in included]
    gaps = []
    if period is not None and instants:
        start = datetime.combine(period[0], datetime.min.time(), timezone.utc)
        days = (instants[0] - start).total_seconds() / 86400
        gaps.append(SeriesGap(None, instants[0], days, "period_start", days > warning_days))
    for earlier, later in zip(instants, instants[1:]):
        days = (later - earlier).total_seconds() / 86400
        gaps.append(SeriesGap(earlier, later, days, "between_dates", days > warning_days))
    if period is not None and instants:
        end = datetime.combine(period[1], datetime.max.time(), timezone.utc)
        days = (end - instants[-1]).total_seconds() / 86400
        gaps.append(SeriesGap(instants[-1], None, days, "period_end", days > warning_days))
    return tuple(gaps)


def _interpretation(dates, common_coverage, gaps, profile, scl_policy):
    restrictions = list(change_policy_restrictions(scl_policy))
    rejected = bool(restrictions)
    included = [item for item in dates if item.included]
    if len(included) < MIN_SERIES_DATES:
        rejected = True
        restrictions.append(f"Moins de {MIN_SERIES_DATES} dates exploitables : série non comparable.")
    if common_coverage < profile.min_comparable_coverage_percent or common_coverage <= 0:
        rejected = True
        restrictions.append("Couverture des pixels communs inférieure au minimum du profil.")
    rules = profile.seasonal_rules
    seasonal_assessed = bool(rules.month_seasons)
    if not seasonal_assessed:
        restrictions.append("Comparabilité saisonnière non évaluée : aucun calendrier dans le profil.")
    elif rules.require_same_season and len({item.season_id for item in included}) > 1:
        rejected = True
        restrictions.append("Saisons différentes ; le profil exige la même saison.")
    if rules.max_interval_days is not None and len(included) >= 2:
        span = (included[-1].acquired_at_utc - included[0].acquired_at_utc).total_seconds() / 86400
        if span > rules.max_interval_days:
            rejected = True
            restrictions.append("Durée de la série supérieure à l'intervalle maximal du profil.")
    if any(gap.exceeds_warning for gap in gaps):
        restrictions.append("Lacunes temporelles supérieures au seuil choisi : évolution sous-échantillonnée.")
    if profile.experimental:
        restrictions.append("Profil expérimental : seuils non validés scientifiquement.")
    restrictions.append("Les statistiques propres à chaque date portent sur des pixels différents "
                        "et ne constituent pas une tendance.")
    restrictions.append("Évolution exploratoire sur pixels communs ; aucune tendance validée. "
                        "Nuages résiduels, saisonnalité, humidité, marées et saturation peuvent contribuer au signal.")
    status = (ScientificStatus.NOT_COMPARABLE if rejected else
              ScientificStatus.NOT_ASSESSED if not seasonal_assessed else
              ScientificStatus.EXPLORATION)
    return InterpretationAssessment(status, tuple(restrictions))


def analyze_ndvi_time_series(
    acquisitions: Sequence[Sentinel2Acquisition],
    *,
    aoi_path: str | Path,
    scientific_profile: ScientificProfile,
    period: tuple[date, date] | None = None,
    gap_warning_days: float = 15.0,
    source_sha256: Mapping[str, Mapping[str, str]] | None = None,
) -> NDVITimeSeriesResult:
    """Compute a traceable NDVI series on one AOI and one common grid.

    A date is retained for common-pixel statistics when its valid AOI coverage
    reaches the profile minimum; excluded dates keep their own statistics and
    an explicit reason. ``source_sha256`` maps product ids to asset digests for
    provenance only; integrity is verified by the caller's manifest adapter.
    """
    profile = scientific_profile
    ordered = _validate_inputs(acquisitions, aoi_path, profile, period, gap_warning_days)
    ordered = [_profile_context(_revalidate(item), profile) for item in ordered]
    policy = profile.scl_policy
    aoi_path = Path(aoi_path)
    aoi_sha256 = hashlib.sha256(aoi_path.read_bytes()).hexdigest()

    with TemporaryDirectory(prefix="geonaf-ndvi-series-") as scratch:
        scratch = Path(scratch)
        inputs = []
        for index, acquisition in enumerate(ordered):
            clipped = {}
            for role, asset in (("b04", acquisition.b04), ("b08", acquisition.b08), ("scl", acquisition.scl)):
                clipped[role] = clip_raster_to_aoi(asset.path, scratch / f"{index}-{role}.tif",
                                                   aoi_path, all_touched=True)
            calibration = acquisition.calibration
            ndvi, profile_raster = calculate_ndvi(
                clipped["b04"], clipped["b08"], None, calibration.red_offset,
                calibration.nir_offset, calibration.quantification_value)
            ndvi_path = _write_temporary_ndvi(scratch / f"{index}-ndvi.tif", ndvi, profile_raster)
            del ndvi
            (scratch / f"{index}-b04.tif").unlink()
            (scratch / f"{index}-b08.tif").unlink()
            inputs.append((ndvi_path, clipped["scl"]))

        grid = build_series_grid([path for path, _ in inputs])
        aoi_pixels = _rasterize_aoi_pixel_mask(grid, aoi_path)
        aoi = load_equal_area_aoi(aoi_path)
        weights = prepare_pixel_area_weights(grid, aoi)

        def read(ndvi_path, scl_path):
            values, raster_valid = _read_ndvi_on_grid(ndvi_path, grid, scl_path, policy)
            valid = raster_valid & scl_policy_mask_on_grid(scl_path, grid, policy) & np.isfinite(values)
            in_range = (values >= -1.0) & (values <= 1.0)
            return values, valid, in_range

        # Pass 1: own statistics, coverage, packed validity masks.
        records, packed = [], []
        for acquisition, (ndvi_path, scl_path) in zip(ordered, inputs):
            values, valid, in_range = read(ndvi_path, scl_path)
            out_of_range = int(np.count_nonzero(valid & ~in_range & aoi_pixels))
            valid &= in_range
            coverage = measure_mask_coverage(valid, grid, aoi, pixel_area_weights=weights)
            own = _summary(values[valid & aoi_pixels])
            included = coverage.coverage_percent >= profile.min_comparable_coverage_percent and own.count > 0
            reason = None if included else (
                f"Couverture valide {coverage.coverage_percent:.2f} % inférieure au minimum "
                f"du profil ({profile.min_comparable_coverage_percent:g} %).")
            calibration = acquisition.calibration
            context = acquisition.seasonal_context
            records.append(dict(
                product_id=acquisition.product_id, acquired_at_utc=_utc(acquisition.acquired_at),
                season_id=context.season_id if context is not None else None,
                calibration=MappingProxyType({"quantification_value": calibration.quantification_value,
                                              "red_offset": calibration.red_offset,
                                              "nir_offset": calibration.nir_offset}),
                valid_coverage_percent=coverage.coverage_percent, valid_area_m2=coverage.area_m2,
                own=own, out_of_range_pixels=out_of_range, included=included,
                exclusion_reason=reason,
                source_sha256=MappingProxyType(dict((source_sha256 or {}).get(acquisition.product_id, {}))),
            ))
            packed.append(np.packbits(valid, axis=None))
            del values, valid, in_range

        shape = (grid.height, grid.width)
        common = aoi_pixels.copy()
        retained = [index for index, record in enumerate(records) if record["included"]]
        for index in retained:
            common &= np.unpackbits(packed[index], count=common.size).reshape(shape).astype(bool)
        if not retained:
            common[:] = False
        common_coverage = measure_mask_coverage(common, grid, aoi, pixel_area_weights=weights)

        # Pass 2: common-pixel statistics, re-reading one date at a time.
        for index, record in enumerate(records):
            record["common"] = None
            if record["included"]:
                values, _, _ = read(*inputs[index])
                record["common"] = _summary(values[common])
                del values

    dates = tuple(SeriesDate(**record) for record in records)
    gaps = _gaps([item for item in dates if item.included], period, gap_warning_days)
    return NDVITimeSeriesResult(
        dates=dates, grid=grid, scl_policy=policy, scientific_profile=profile,
        aoi_sha256=aoi_sha256, period=period,
        common_pixel_count=int(np.count_nonzero(common)),
        common_area_m2=common_coverage.area_m2,
        common_coverage_percent=common_coverage.coverage_percent,
        gaps=gaps, gap_warning_days=float(gap_warning_days),
        interpretation=_interpretation(dates, common_coverage.coverage_percent, gaps, profile, policy),
        computed_at_utc=datetime.now(timezone.utc),
    )


def series_csv(result: NDVITimeSeriesResult) -> str:
    """CSV with a ``#`` provenance header; no local paths are written."""
    import csv
    import io

    profile = result.scientific_profile
    output = io.StringIO()
    period = (f"{result.period[0].isoformat()}/{result.period[1].isoformat()}"
              if result.period else "non précisée")
    header = {
        "geonaf_export": "ndvi-time-series-v1",
        "computed_at_utc": result.computed_at_utc.isoformat(),
        "source": "Copernicus Sentinel-2 L2A (B04, B08, SCL, MTD_MSIL2A.xml)",
        "profile": f"{profile.profile_id} {profile.version}",
        "profile_sha256": profile.fingerprint,
        "aoi_id": profile.aoi_id,
        "aoi_sha256": result.aoi_sha256,
        "period_utc": period,
        "scl_policy": result.scl_policy,
        "grid": f"{result.grid.crs.to_string()} res={result.grid.resolution[0]:g}x{result.grid.resolution[1]:g} "
                f"size={result.grid.width}x{result.grid.height}",
        "common_coverage_percent": repr(result.common_coverage_percent),
        "common_pixel_count": str(result.common_pixel_count),
        "status": result.interpretation.status.value,
        "scope": result.interpretation_scope,
    }
    for key, value in header.items():
        output.write(f"# {key}: {value}\n")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow([
        "acquired_at_utc", "product_id", "season_id", "included", "exclusion_reason",
        "valid_coverage_percent", "own_pixel_count", "own_mean", "own_median", "own_std",
        "common_pixel_count", "common_mean", "common_median", "common_std",
        "out_of_range_pixels", "boa_quantification", "red_offset", "nir_offset",
        "b04_sha256", "b08_sha256", "scl_sha256", "calibration_xml_sha256",
    ])
    for item in result.dates:
        common = item.common
        writer.writerow([
            item.acquired_at_utc.isoformat(), item.product_id, item.season_id or "",
            item.included, item.exclusion_reason or "", repr(item.valid_coverage_percent),
            item.own.count, item.own.mean, item.own.median, item.own.std,
            common.count if common else "", common.mean if common else "",
            common.median if common else "", common.std if common else "",
            item.out_of_range_pixels, item.calibration["quantification_value"],
            item.calibration["red_offset"], item.calibration["nir_offset"],
            *(item.source_sha256.get(role, "") for role in ("b04", "b08", "scl", "calibration_xml")),
        ])
    return output.getvalue()
