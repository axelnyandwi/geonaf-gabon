"""Reliability rules and temporal comparability: explicit rules, never an inferred change."""

from __future__ import annotations

from src.indicators.model import LOW, MEDIUM, NOT_ASSESSED, UNAVAILABLE, ChangeAssessment, TerritorialIndicator
from src.services.multisource_demo import CLASSIFICATION, COLLABORATIVE

OSM_RELIABILITY_REASONS = ("Complétude OpenStreetMap non évaluée sur la zone d'étude : valeur minimale cartographiée.",)


def landcover_reliability(descriptor, coverage, profile):
    """(level, reasons, warning or None): producer-declared accuracy, low below the profile's coverage."""
    reasons = ["Exactitude déclarée par le producteur, non vérifiée sur la zone d'étude.",
               f"Référentiel {descriptor.temporal.start_utc:%Y}, antérieur aux observations récentes."]
    if coverage < profile.min_coverage_percent:
        reasons.append(f"Couverture {coverage:.1f} % < {profile.min_coverage_percent:g} % de la zone d'étude.")
        return LOW, tuple(reasons), (f"WorldCover ne couvre que {coverage:.1f} % de la zone d'étude : "
                                     "surfaces sous-estimées.")
    return MEDIUM, tuple(reasons), None


def osm_reliability():
    """OpenStreetMap completeness is unknown: the level is never assessed."""
    return NOT_ASSESSED, OSM_RELIABILITY_REASONS


def _reference_instant(indicator):
    dates = indicator.dates
    return dates.reference_at_utc or dates.observation_end_utc


def assess_change(before: TerritorialIndicator, after: TerritorialIndicator) -> ChangeAssessment:
    """Difference ``after - before`` only when both values are strictly comparable; never inferred."""
    reasons = []
    if before.indicator_id != after.indicator_id or before.unit != after.unit:
        reasons.append("Indicateurs ou unités différents.")
    if UNAVAILABLE in (before.status, after.status) or None in (before.value, after.value):
        reasons.append("Valeur indisponible à l'une des dates.")
    if before.aoi_sha256 != after.aoi_sha256:
        reasons.append("Zones d'étude différentes.")
    if before.profile_fingerprint != after.profile_fingerprint:
        reasons.append("Profils d'indicateurs (regroupements) différents.")
    if before.source is None or after.source is None or before.dates is None or after.dates is None:
        reasons.append("Source ou dates non documentées.")
    else:
        if before.source.source_id != after.source.source_id:
            reasons.append("Sources différentes.")
        elif before.source.nature == CLASSIFICATION and before.source.version != after.source.version:
            reasons.append(f"Versions {before.source.version} et {after.source.version} produites par des "
                           "algorithmes différents : l'écart n'est pas un changement du territoire.")
        start, end = _reference_instant(before), _reference_instant(after)
        if before.source.fingerprint == after.source.fingerprint or start == end:
            reasons.append("Même référentiel aux deux dates : aucune évolution mesurable.")
        elif start is None or end is None or end < start:
            reasons.append("Ordre chronologique des référentiels non établi.")
    for item in (before, after):
        if item.coverage_percent is not None and item.coverage_percent < 99.0:
            reasons.append(f"Couverture insuffisante ({item.coverage_percent:.1f} %).")
    if reasons:
        return ChangeAssessment(after.indicator_id, False, "non comparable", None, after.unit, tuple(reasons))
    collaborative = after.source.nature == COLLABORATIVE
    kind = ("évolution de la cartographie OpenStreetMap (ni nouvelles constructions ni nouvelles voies)"
            if collaborative else "évolution entre deux référentiels comparables")
    notes = (("Un objet ajouté à la base peut exister sur le terrain depuis longtemps.",) if collaborative else ())
    return ChangeAssessment(after.indicator_id, True, kind, after.value - before.value, after.unit, notes)
