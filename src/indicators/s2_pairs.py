"""Pairs of verified Sentinel-2 acquisitions of a zone: selection, then class transitions; no UI, no network.

Builds on the L3 inventory (``s2_vegetation_inventory``): its acquisitions and
their individual usable coverage. Nothing is downloaded.

Selection, in two separate steps so that each can be timed and tested:

1. :func:`plan_pairs` (pure, no file read): every chronological pair of
   inventoried acquisitions. The common surface lies inside each date's usable
   surface, so ``min(individual coverage)`` is an **upper bound** of the common
   coverage. A pair whose bound is below the profile minimum cannot pass and is
   rejected without computation; a bound above it proves nothing. Multi-tile
   mosaics are rejected (the historical pair chain reads one tile per date).
   Remaining candidates are ordered by recency: later acquisition first, then
   the later of the earlier acquisitions.
2. :func:`s2_pair_selection`: resource guard first, then each candidate in that
   order, at most ``max_pairs`` of them, through the historical chain
   (``analyze_local_akanda``: SHA-256 of every asset, calibrated NDVI, SCL policy
   of the scientific profile, common mask), :func:`measure_pair_result` and
   :func:`pair_assessment`. Evaluation stops at the first publishable pair, the
   default one.

Only numbers are kept: the chain's arrays are released after each pair.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import re

from src.indicators.profiles import S2_NDVI_TRANSITION_V0, TransitionProfile
from src.indicators.quality import PairAssessment, check_vegetation_scl_policy, pair_assessment, same_lattice_pair
from src.indicators.presentation import fr
from src.indicators.s2_inventory import MAX_ACQUISITIONS, S2VegetationEstimate, S2VegetationInventory
from src.indicators.transitions_s2 import PairMeasure, interval_days, measure_pair_result

PUBLISHABLE = "publiable"
NOT_PUBLISHABLE = "non publiable"          # Computed; refused by a gate or an error (diagnosis only).
REJECTED = "écartée avant calcul"          # Upper bound, mosaic: never computed.
NOT_EVALUATED = "non évaluée"              # Evaluation limit, resource guard, or a more recent pair retained.
MAX_EVALUATED_PAIRS = 3
BOUND_RTOL = 1e-9                          # Float sums of the same weights on two grids of the same lattice.


@dataclass(frozen=True)
class PairCandidate:
    earlier: S2VegetationEstimate
    later: S2VegetationEstimate
    coverage_upper_bound_percent: float     # min(individual coverage): never a proof of comparability.
    status: str
    reasons: tuple[str, ...]
    measure: PairMeasure | None = None      # None when not computed, or on misaligned grids.
    assessment: PairAssessment | None = None
    seasonal_status: str | None = None      # Comparability rules of the scientific profile (historical chain).
    seasonal_notes: tuple[str, ...] = ()

    @property
    def interval_days(self) -> float:
        return interval_days(self.earlier.acquired_at_utc, self.later.acquired_at_utc)

    @property
    def label(self) -> str:
        return f"{self.earlier.acquired_at_utc:%d/%m/%Y} → {self.later.acquired_at_utc:%d/%m/%Y}"


@dataclass(frozen=True)
class S2PairSelection:
    aoi_id: str
    aoi_sha256: str
    scientific_profile_id: str
    scientific_profile_version: str
    scientific_profile_fingerprint: str
    scl_policy: str
    change_threshold: float                 # ΔNDVI variation threshold of the historical classes (not used here).
    profile: TransitionProfile
    pairs: tuple[PairCandidate, ...]        # Evaluation order (most recent first), rejected pairs last.
    max_pairs: int
    notes: tuple[str, ...] = ()

    def default_pair(self) -> PairCandidate | None:
        """Most recent publishable pair; None when there is none."""
        return next((p for p in self.pairs if p.status == PUBLISHABLE), None)


def plan_pairs(inventory: S2VegetationInventory, profile: TransitionProfile = S2_NDVI_TRANSITION_V0
               ) -> tuple[tuple[PairCandidate, ...], tuple[PairCandidate, ...]]:
    """(candidates by recency, pairs rejected before computation); reads no file."""
    estimates = sorted(inventory.estimates, key=lambda e: e.acquired_at_utc)
    candidates, rejected = [], []
    minimum = profile.min_common_coverage_percent
    for j, later in enumerate(estimates):
        for earlier in estimates[:j]:
            bound = min(earlier.measure.observed_coverage_percent, later.measure.observed_coverage_percent)
            reasons = []
            if interval_days(earlier.acquired_at_utc, later.acquired_at_utc) <= 0:
                reasons.append("Acquisitions au même instant : comparaison impossible.")
            if len(earlier.product_ids) > 1 or len(later.product_ids) > 1:
                reasons.append("Mosaïque de plusieurs tuiles : la chaîne de comparaison historique ne lit qu'une "
                               "tuile par date.")
            if bound < minimum:
                reasons.append(f"Borne supérieure de couverture commune {fr(bound, 1)} % < {fr(minimum)} % "
                               f"(min des couvertures individuelles) : couverture commune nécessairement "
                               "insuffisante, paire non calculée.")
            pair = PairCandidate(earlier, later, bound, REJECTED if reasons else NOT_EVALUATED, tuple(reasons))
            (rejected if reasons else candidates).append(pair)
    candidates.sort(key=lambda p: (p.later.acquired_at_utc, p.earlier.acquired_at_utc), reverse=True)
    rejected.sort(key=lambda p: (p.later.acquired_at_utc, p.earlier.acquired_at_utc), reverse=True)
    return tuple(candidates), tuple(rejected)


def s2_pairs_key(data_dir, aoi_id: str, *, year: int = 2025, scientific_profile=None,
                 profile: TransitionProfile = S2_NDVI_TRANSITION_V0, max_pairs: int = MAX_EVALUATED_PAIRS,
                 max_acquisitions: int = MAX_ACQUISITIONS) -> tuple:
    """Cheap cache key: the inventory key (zone digest, acquisitions, scientific and vegetation profiles), the
    transition profile and the evaluation limit. Not an integrity check (digests are verified on a miss)."""
    from src.indicators.s2_inventory import s2_inventory_key

    return ("s2-pairs-v1", s2_inventory_key(data_dir, aoi_id, year=year, scientific_profile=scientific_profile,
                                            profile=profile.vegetation, max_acquisitions=max_acquisitions),
            profile.fingerprint, max_pairs)


def _observation_groups(data_dir):
    from src.services.local_preview import group_local_observations, list_local_observations

    return {tuple(o.product_id for o in group): group for group in group_local_observations(
        list_local_observations(data_dir))}


def _evaluate(pair, groups, data_dir, scientific_profile, profile, zone) -> PairCandidate:
    from src.services.local_preview import LocalPreviewError, analyze_local_akanda

    earlier, later = groups.get(pair.earlier.product_ids), groups.get(pair.later.product_ids)
    if earlier is None or later is None:
        return _with(pair, NOT_PUBLISHABLE, ("Acquisition de l'inventaire introuvable localement : paire non "
                                             "calculée.",))
    try:
        result = analyze_local_akanda(earlier[0], later[0], data_dir, scientific_profile=scientific_profile)
        aligned = same_lattice_pair(result.earlier.asset_metadata["b04"], result.later.asset_metadata["b04"],
                                    result.common_grid)
        measure = measure_pair_result(result, profile, zone)
        seasonal = result.seasonal_assessment
        seasonal_status, seasonal_notes = seasonal.status, tuple(seasonal.warnings)
        del result                                       # Arrays are not kept.
    except (LocalPreviewError, ValueError) as error:     # Public messages only (no path, no exception text).
        return _with(pair, NOT_PUBLISHABLE, (str(error),))
    assessment = pair_assessment(measure, profile, interval_days=pair.interval_days, aligned=aligned)
    reasons = assessment.reasons
    available = assessment.available
    if measure.common_coverage_percent > pair.coverage_upper_bound_percent * (1 + BOUND_RTOL):
        available = False
        reasons = ("Couverture commune réelle supérieure à la borne de l'inventaire : entrées incohérentes, "
                   "transitions non publiées.",)
    return PairCandidate(pair.earlier, pair.later, pair.coverage_upper_bound_percent,
                         PUBLISHABLE if available else NOT_PUBLISHABLE, reasons,
                         measure if aligned else None, assessment, seasonal_status, seasonal_notes)


def _with(pair, status, reasons):
    return PairCandidate(pair.earlier, pair.later, pair.coverage_upper_bound_percent, status, tuple(reasons))


def s2_pair_selection(data_dir, aoi_id: str, *, inventory: S2VegetationInventory | None = None,
                      year: int = 2025, scientific_profile=None,
                      profile: TransitionProfile = S2_NDVI_TRANSITION_V0, max_pairs: int = MAX_EVALUATED_PAIRS,
                      limits=None, available_bytes: int | None = -1) -> S2PairSelection:
    """Transition pairs of the GEONAF zone ``aoi_id`` (see module docstring).

    ``inventory`` is the L3 inventory of the same zone and profiles (computed
    when omitted). Refused before any computation: an SCL policy accepting
    clouds, shadows or defective pixels, an inventory of another zone or other
    profiles (``ValueError``), and a zone over the resource limits (no pair
    evaluated, reasons in ``notes``).
    """
    import geopandas as gpd

    from src.geo.change_analysis import load_equal_area_aoi
    from src.geo.scientific_profile import REFERENCE_PROFILES
    from src.indicators.s2_inventory import s2_vegetation_inventory
    from src.services.local_preview import LocalPreviewError
    from src.services.resource_guards import assess_zone

    if isinstance(max_pairs, bool) or not isinstance(max_pairs, int) or max_pairs < 1:
        raise ValueError("Nombre maximal de paires évaluées invalide.")
    data_dir = Path(data_dir)
    scientific_profile = scientific_profile or REFERENCE_PROFILES[aoi_id]
    if scientific_profile.aoi_id != aoi_id:
        raise ValueError("Profil scientifique d'une autre zone d'étude.")
    check_vegetation_scl_policy(scientific_profile.scl_policy)
    if inventory is None:
        inventory = s2_vegetation_inventory(data_dir, aoi_id, year=year, scientific_profile=scientific_profile,
                                            profile=profile.vegetation, limits=limits,
                                            available_bytes=available_bytes)
    aoi_path = data_dir / "aoi" / f"{aoi_id}.geojson"
    aoi_sha256 = hashlib.sha256(aoi_path.read_bytes()).hexdigest()
    if (inventory.aoi_id, inventory.aoi_sha256) != (aoi_id, aoi_sha256):
        raise ValueError("Inventaire d'une autre zone d'étude ou d'une autre géométrie.")
    if inventory.scientific_profile_fingerprint != scientific_profile.fingerprint \
            or inventory.profile.fingerprint != profile.vegetation.fingerprint:
        raise ValueError("Inventaire calculé avec d'autres profils.")
    common = dict(aoi_id=aoi_id, aoi_sha256=aoi_sha256, scientific_profile_id=scientific_profile.profile_id,
                  scientific_profile_version=scientific_profile.version,
                  scientific_profile_fingerprint=scientific_profile.fingerprint,
                  scl_policy=scientific_profile.scl_policy, change_threshold=scientific_profile.change_threshold,
                  profile=profile, max_pairs=max_pairs)
    candidates, rejected = plan_pairs(inventory, profile)
    notes = list(inventory.notes)
    if inventory.failures:
        notes.append(f"{len(inventory.failures)} acquisition(s) en échec dans l'inventaire : exclue(s) des paires.")
    if not candidates:
        notes.append("Aucune paire candidate : moins de deux acquisitions, ou couverture commune nécessairement "
                     "insuffisante.")
        return S2PairSelection(**common, pairs=rejected, notes=tuple(notes))
    geometry = gpd.read_file(aoi_path).to_crs("EPSG:4326").geometry.union_all()
    guard = assess_zone(geometry, limits=limits, osm_wanted=False, available_bytes=available_bytes)
    if not guard.allowed:
        reasons = tuple("Calcul de paire refusé avant traitement : " + check.reason for check in guard.refusals)
        return S2PairSelection(**common, pairs=tuple(_with(p, NOT_EVALUATED, reasons) for p in candidates) + rejected,
                               notes=tuple(notes) + reasons)
    try:
        groups = _observation_groups(data_dir)
    except LocalPreviewError as error:
        return S2PairSelection(**common, pairs=tuple(_with(p, NOT_EVALUATED, (str(error),)) for p in candidates)
                               + rejected, notes=tuple(notes) + (str(error),))
    zone = load_equal_area_aoi(aoi_path)
    evaluated, retained, computed = [], None, 0
    for pair in candidates:
        if retained is not None:
            evaluated.append(_with(pair, NOT_EVALUATED, (f"Paire plus récente retenue ({retained.label}).",)))
        elif computed >= max_pairs:
            evaluated.append(_with(pair, NOT_EVALUATED, (f"Limite de {max_pairs} paire(s) évaluée(s) atteinte "
                                                         "(borne de ressources).",)))
        else:
            computed += 1
            evaluated.append(_evaluate(pair, groups, data_dir, scientific_profile, profile, zone))
            if evaluated[-1].status == PUBLISHABLE:
                retained = evaluated[-1]
    return S2PairSelection(**common, pairs=tuple(evaluated) + rejected, notes=tuple(notes))


SATELLITES = {"S2A": "Sentinel-2A", "S2B": "Sentinel-2B", "S2C": "Sentinel-2C", "S2D": "Sentinel-2D"}


def satellite(product_id: str) -> str | None:
    """Platform from the product identifier (ESA naming convention), None when it does not follow it."""
    match = re.match(r"(S2[A-D])_", product_id)
    return SATELLITES[match.group(1)] if match else None


def pair_provenance(pair: PairCandidate, selection: S2PairSelection) -> dict:
    """Facts behind one pair, JSON-serialisable and without any local path."""
    from src.services.ndvi_observation import relative_orbit

    profile = selection.profile

    def acquisition(estimate):
        return {"acquired_at_utc": estimate.acquired_at_utc.isoformat(), "ingested_at_utc": estimate.ingested_at_utc,
                "product_ids": list(estimate.product_ids), "source": estimate.source,
                "satellites": sorted({s for s in map(satellite, estimate.product_ids) if s}),
                "relative_orbits": sorted({o for o in map(relative_orbit, estimate.product_ids) if o}),
                "absolute_orbit": None,           # Not recorded in the verified manifests.
                "individual_coverage_percent": estimate.measure.observed_coverage_percent,
                "tiles": [{key: tile[key] for key in ("product_id", "collection", "stac_item", "manifest_sha256",
                                                      "scl_sha256", "subset")} for tile in estimate.provenance]}

    measure, assessment = pair.measure, pair.assessment
    return {
        "aoi_id": selection.aoi_id, "aoi_sha256": selection.aoi_sha256, "status": pair.status,
        "earlier": acquisition(pair.earlier), "later": acquisition(pair.later), "interval_days": pair.interval_days,
        "profiles": {
            "scientific": {"id": selection.scientific_profile_id, "version": selection.scientific_profile_version,
                           "fingerprint": selection.scientific_profile_fingerprint, "scl_policy": selection.scl_policy,
                           "ndvi_variation_threshold": selection.change_threshold},
            "vegetation": {"id": profile.vegetation.profile_id, "version": profile.vegetation.version,
                           "fingerprint": profile.vegetation.fingerprint},
            "transition": {"id": profile.profile_id, "version": profile.version, "fingerprint": profile.fingerprint}},
        "ndvi_class_threshold": profile.threshold,
        "coverage_upper_bound_percent": pair.coverage_upper_bound_percent,
        "common_coverage_percent": None if measure is None else measure.common_coverage_percent,
        "common_area_m2": None if measure is None else measure.common_m2,
        "seasonal_comparability": pair.seasonal_status, "comparability_notes": list(pair.seasonal_notes),
        "reasons": list(pair.reasons), "warnings": [] if assessment is None else list(assessment.warnings),
        "scientific_status": profile.scientific_status if assessment is None else assessment.scientific_status,
        "reliability": None if assessment is None else assessment.reliability,
    }
