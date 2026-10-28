"""Indicator records: every value carries its unit, zone, dates, source, coverage, method, reliability and limits."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from src.indicators.profiles import IndicatorProfile

AVAILABLE, UNAVAILABLE = "disponible", "indisponible"
HIGH, MEDIUM, LOW, NOT_ASSESSED = "élevée", "moyenne", "faible", "non évaluée"
AOI_NATURE = "Géométrie de référence"


@dataclass(frozen=True)
class IndicatorDates:
    """Three dates never merged: what was observed, which dataset state, when it was ingested."""

    observation_start_utc: datetime | None   # None: unknown (e.g. OSM objects' field date).
    observation_end_utc: datetime | None
    observation_note: str
    reference_label: str                     # Version or database state of the reference dataset.
    reference_at_utc: datetime | None        # Database timestamp when one exists (OSM).
    ingested_at_utc: datetime | None         # Retrieval by GEONAF.


@dataclass(frozen=True)
class IndicatorSource:
    source_id: str
    product: str
    version: str
    nature: str
    attribution: str
    fingerprint: str | None


@dataclass(frozen=True)
class BreakdownItem:
    key: str
    label: str
    value: float
    unit: str
    share_percent: float | None = None
    detail: tuple[tuple[str, float], ...] = ()   # e.g. (highway value, km) or (class label, km²).


@dataclass(frozen=True)
class TerritorialIndicator:
    indicator_id: str
    label: str
    value: float | None
    unit: str
    status: str
    aoi_id: str
    aoi_sha256: str
    dates: IndicatorDates | None
    source: IndicatorSource | None
    coverage_percent: float | None
    method: str
    reliability: str
    reliability_reasons: tuple[str, ...]
    limits: tuple[str, ...]
    profile_fingerprint: str
    shares: tuple[tuple[str, float], ...] = ()          # (denominator, percent)
    breakdown: tuple[BreakdownItem, ...] = ()


@dataclass(frozen=True)
class ChangeAssessment:
    indicator_id: str
    comparable: bool
    kind: str
    delta: float | None
    unit: str
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class TerritorialIndicators:
    aoi_id: str
    aoi_label: str
    aoi_sha256: str
    aoi_area_km2: float
    profile: IndicatorProfile
    indicators: tuple[TerritorialIndicator, ...]
    temporal_notes: tuple[str, ...]
    missing: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    computed_at_utc: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def get(self, indicator_id: str) -> TerritorialIndicator:
        return next(item for item in self.indicators if item.indicator_id == indicator_id)
