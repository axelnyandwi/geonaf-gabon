"""Sentinel-2 vegetation estimates of a zone from verified local acquisitions; acquisition side, no UI, no network.

Reuses the existing chain: ``list_local_observations`` / ``group_local_observations``
(verified manifests), ``analyze_local_group_with_scl`` (SHA-256 of every asset,
calibrated NDVI, SCL policy of the scientific profile). For a single tile the
SCL classes come from the clipped file that built the NDVI validity mask and
are checked against it (``check_scl_consistency``); a multi-tile mosaic keeps
its main figures but gets no detailed breakdown.

Only measurements are kept: NDVI, masks and weights are released after each
acquisition, so a cached inventory holds a few numbers per date.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
from pathlib import Path

from src.indicators.profiles import S2_NDVI_VEGETATION_V0, VegetationIndexProfile
from src.indicators.quality import (
    VegetationEstimateAssessment, check_scl_consistency, check_vegetation_scl_policy,
    vegetation_estimate_assessment,
)
from src.indicators.vegetation_s2 import VegetationMeasure, measure_observed_vegetation

BREAKDOWN_AVAILABLE, BREAKDOWN_UNAVAILABLE = "ventilée", "ventilation non disponible"
MAX_ACQUISITIONS = 12


@dataclass(frozen=True)
class S2VegetationEstimate:
    product_ids: tuple[str, ...]
    acquired_at_utc: datetime
    ingested_at_utc: str | None
    measure: VegetationMeasure                 # Kept for diagnosis even when the estimate is not published.
    assessment: VegetationEstimateAssessment
    breakdown_status: str
    provenance: tuple[dict, ...]               # Verified manifest facts per tile (no local path).
    calibration: dict
    source: str
    mosaic_notes: tuple[str, ...] = ()

    @property
    def fingerprint(self) -> str:
        """Identity of the inputs (manifests and SCL digests) behind this estimate."""
        text = "|".join(f"{p['product_id']}:{p['manifest_sha256']}:{p['scl_sha256']}" for p in self.provenance)
        return hashlib.sha256(text.encode()).hexdigest()


@dataclass(frozen=True)
class S2VegetationInventory:
    aoi_id: str
    aoi_sha256: str
    year: int
    scientific_profile_id: str
    scientific_profile_version: str
    scientific_profile_fingerprint: str
    scl_policy: str
    profile: VegetationIndexProfile
    estimates: tuple[S2VegetationEstimate, ...]      # Chronological.
    failures: tuple[tuple[str, str], ...] = ()       # (acquisition, public reason).
    notes: tuple[str, ...] = ()

    def default_estimate(self) -> S2VegetationEstimate | None:
        """Most recent acquisition whose estimate may be published; None when there is none."""
        return next((e for e in reversed(self.estimates) if e.assessment.available), None)


def s2_inventory_key(data_dir, aoi_id: str, *, year: int = 2025, scientific_profile=None,
                     profile: VegetationIndexProfile = S2_NDVI_VEGETATION_V0,
                     max_acquisitions: int = MAX_ACQUISITIONS) -> tuple:
    """Cheap cache key: zone digest, profiles and, per datatake, file identities and manifest digests.

    Not an integrity check: every asset SHA-256 is still verified when the
    inventory is computed (cache miss). Any edited, added or removed file of
    the year changes the key.
    """
    from src.geo.scientific_profile import REFERENCE_PROFILES
    from src.services.local_preview import group_local_observations, list_local_observations, local_group_key

    data_dir = Path(data_dir)
    scientific_profile = scientific_profile or REFERENCE_PROFILES[aoi_id]
    aoi_path = data_dir / "aoi" / f"{aoi_id}.geojson"
    groups = group_local_observations([o for o in list_local_observations(data_dir) if o.acquired_at.year == year])
    return ("s2-vegetation-v1", aoi_id, hashlib.sha256(aoi_path.read_bytes()).hexdigest(), year, max_acquisitions,
            scientific_profile.fingerprint, profile.fingerprint,
            tuple(local_group_key(group, data_dir, scientific_profile) for group in groups))


def _label(group) -> str:
    return f"{group[0].acquired_at:%d/%m/%Y %H:%M} UTC ({' + '.join(o.product_id for o in group)})"


def s2_vegetation_inventory(data_dir, aoi_id: str, *, year: int = 2025, scientific_profile=None,
                            profile: VegetationIndexProfile = S2_NDVI_VEGETATION_V0,
                            max_acquisitions: int = MAX_ACQUISITIONS, limits=None,
                            available_bytes: int | None = -1) -> S2VegetationInventory:
    """Estimates for every verified acquisition of ``year`` over the GEONAF zone ``aoi_id``.

    Refused before any computation: an SCL policy accepting clouds, shadows or
    defective pixels (``ValueError``) and a zone over the resource limits
    (inventory without estimates, reasons in ``notes``). An unreadable or
    altered acquisition is listed in ``failures``; the others are computed.
    """
    import geopandas as gpd

    from src.geo.change_analysis import load_equal_area_aoi
    from src.geo.scientific_profile import REFERENCE_PROFILES
    from src.services.local_preview import (
        LocalPreviewError, analyze_local_group_with_scl, group_local_observations, list_local_observations,
    )
    from src.services.resource_guards import assess_zone

    data_dir = Path(data_dir)
    scientific_profile = scientific_profile or REFERENCE_PROFILES[aoi_id]
    if scientific_profile.aoi_id != aoi_id:
        raise ValueError("Profil scientifique d'une autre zone d'étude.")
    check_vegetation_scl_policy(scientific_profile.scl_policy)
    aoi_path = data_dir / "aoi" / f"{aoi_id}.geojson"
    zone = load_equal_area_aoi(aoi_path)
    common = dict(aoi_id=aoi_id, aoi_sha256=hashlib.sha256(aoi_path.read_bytes()).hexdigest(), year=year,
                  scientific_profile_id=scientific_profile.profile_id,
                  scientific_profile_version=scientific_profile.version,
                  scientific_profile_fingerprint=scientific_profile.fingerprint,
                  scl_policy=scientific_profile.scl_policy, profile=profile)
    geometry = gpd.read_file(aoi_path).to_crs("EPSG:4326").geometry.union_all()
    guard = assess_zone(geometry, limits=limits, osm_wanted=False, available_bytes=available_bytes)
    if not guard.allowed:
        return S2VegetationInventory(**common, estimates=(), notes=tuple(
            "Calcul Sentinel-2 refusé avant traitement : " + check.reason for check in guard.refusals))
    try:
        observations = [o for o in list_local_observations(data_dir) if o.acquired_at.year == year]
    except LocalPreviewError as error:
        return S2VegetationInventory(**common, estimates=(), notes=(str(error),))
    groups = group_local_observations(observations)
    notes = []
    if not groups:
        notes.append(f"Aucune acquisition Sentinel-2 locale vérifiée pour {year}.")
    if len(groups) > max_acquisitions:
        notes.append(f"{len(groups)} acquisitions en {year} : seules les {max_acquisitions} plus récentes sont "
                     "calculées (borne de ressources).")
        groups = groups[-max_acquisitions:]
    estimates, failures = [], []
    for group in groups:
        try:
            observation, scl, provenance = analyze_local_group_with_scl(group, data_dir,
                                                                        scientific_profile=scientific_profile)
            if scl is not None:
                check_scl_consistency(observation, scl)
            measure = measure_observed_vegetation(observation, profile, zone,
                                                  scl=None if scl is None else (scl.classes, scl.valid))
        except (LocalPreviewError, ValueError) as error:     # Public messages only (no path, no exception text).
            failures.append((_label(group), str(error)))
            continue
        estimates.append(S2VegetationEstimate(
            product_ids=tuple(p["product_id"] for p in provenance), acquired_at_utc=observation.acquired_at_utc,
            ingested_at_utc=max((p["ingested_at_utc"] for p in provenance if p["ingested_at_utc"]), default=None),
            measure=measure, assessment=vegetation_estimate_assessment(measure, profile),
            breakdown_status=BREAKDOWN_AVAILABLE if scl is not None else BREAKDOWN_UNAVAILABLE,
            provenance=provenance, calibration=observation.calibration, source=observation.source,
            mosaic_notes=observation.mosaic_notes))
        del observation, scl                                   # Arrays are not kept.
    return S2VegetationInventory(**common, estimates=tuple(estimates), failures=tuple(failures), notes=tuple(notes))
