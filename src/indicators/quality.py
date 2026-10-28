"""Reliability rules and temporal comparability: explicit rules, never an inferred change."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from src.data.cloud_mask import (
    SCL_CLOUD_SHADOW_CLASSES, SCL_DEFECTIVE_CLASSES, SCL_MASK_POLICIES, scl_mask_from_values,
)
from src.indicators.model import (
    HIGH, LOW, MEDIUM, NOT_ASSESSED, UNAVAILABLE, ChangeAssessment, TerritorialIndicator,
)
from src.indicators.profiles import VALIDATED
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


RELIABILITY_ORDER = (LOW, MEDIUM, HIGH)
# A vegetation estimate must not count clouds, shadows or defective pixels as observed ground.
FORBIDDEN_VEGETATION_SCL = SCL_DEFECTIVE_CLASSES | SCL_CLOUD_SHADOW_CLASSES


def check_vegetation_scl_policy(policy: str) -> None:
    """Refuse an SCL policy that accepts clouds, shadows, cirrus or defective pixels."""
    if policy not in SCL_MASK_POLICIES:
        raise ValueError(f"Politique SCL inconnue : {policy}.")
    accepted = SCL_MASK_POLICIES[policy] & FORBIDDEN_VEGETATION_SCL
    if accepted:
        raise ValueError(f"Politique SCL « {policy} » refusée pour l'estimation de végétation : elle accepte les "
                         f"classes {', '.join(map(str, sorted(accepted)))} (nuages, ombres ou pixels défectueux).")


def check_scl_consistency(observation, scl) -> None:
    """The SCL classes must be those that built ``observation.valid`` (same product, grid and policy)."""
    shape = (observation.grid.height, observation.grid.width)
    if scl.product_id != observation.product_id:
        raise ValueError("SCL d'un autre produit que l'observation NDVI.")
    if np.shape(scl.classes) != shape or np.shape(scl.valid) != shape:
        raise ValueError("SCL sur une autre grille que l'observation NDVI.")
    accepted = scl_mask_from_values(np.ma.array(scl.classes, mask=~scl.valid), observation.scl_policy, scl.valid)
    if np.any(observation.valid & ~accepted):
        raise ValueError("Pixels NDVI valides hors de la politique SCL : SCL incohérent avec l'observation.")


@dataclass(frozen=True)
class VegetationEstimateAssessment:
    """Whether a Sentinel-2 vegetation estimate may be published for the zone, and how reliable it is."""

    available: bool                 # False: diagnosis only, the territorial estimate is « Non disponible ».
    reliability: str
    reasons: tuple[str, ...]
    scientific_status: str          # From the vegetation profile (experimental until validated).


def vegetation_estimate_assessment(measure, profile) -> VegetationEstimateAssessment:
    """Availability by usable coverage, reliability capped by the profile; never claims representativeness."""
    coverage = measure.observed_coverage_percent
    status = profile.scientific_status
    if measure.observed_m2 <= 0:
        return VegetationEstimateAssessment(False, NOT_ASSESSED, (
            "Aucune surface observée exploitable sur la zone d'étude.",), status)
    if coverage < profile.min_observed_coverage_percent:
        return VegetationEstimateAssessment(False, NOT_ASSESSED, (
            f"Couverture exploitable {coverage:.1f} % < {profile.min_observed_coverage_percent:g} % "
            f"(profil {profile.profile_id} {profile.version}) : estimation territoriale non publiée, "
            "acquisition consultable pour diagnostic.",), status)
    reasons = [f"Couverture exploitable {coverage:.1f} % de la zone d'étude ; la surface non observée n'est pas "
               "estimée et le seuil de couverture ne garantit pas la représentativité spatiale."]
    if status != VALIDATED:
        reasons.insert(0, f"Méthode expérimentale non validée scientifiquement (NDVI ≥ {profile.threshold:g}, "
                          "seuil non calibré localement).")
    # Never above « moyenne »: even a validated threshold describes only the observed part of the zone.
    level = RELIABILITY_ORDER[min(RELIABILITY_ORDER.index(MEDIUM), RELIABILITY_ORDER.index(profile.reliability_cap))]
    return VegetationEstimateAssessment(True, level, tuple(reasons), status)


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
