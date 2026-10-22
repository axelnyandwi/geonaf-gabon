"""Read verified local acquisitions for the Akanda prototype; no network calls."""

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from pathlib import Path

from src.data.acquisition import create_sentinel2_acquisition
from src.data.acquisition_manifest import validate_acquisition_manifest
from src.services.temporal_analysis import analyze_temporal_vegetation_change
from src.geo.change_analysis import DEFAULT_CHANGE_THRESHOLD
from src.geo.scientific_profile import ScientificProfile


class LocalPreviewError(ValueError):
    """Public error without source paths or underlying exception details."""


@dataclass(frozen=True)
class LocalObservation:
    acquired_at: datetime
    manifest_path: Path
    product_id: str = ''


def _manifest(path):
    manifest = json.loads(path.read_text(encoding="utf-8"))
    validate_acquisition_manifest(manifest)
    integrity = manifest.get("integrity", {})
    if integrity.get("mode") != "strict" or any(
        record.get("status") != "verified"
        for record in integrity.get("source_assets", {}).values()
    ):
        raise ValueError("Intégrité source non vérifiée.")
    return manifest


def list_local_observations(data_dir: Path) -> tuple[LocalObservation, ...]:
    """Expose every verified local acquisition, retaining its exact UTC date."""
    try:
        observations = []
        for path in sorted((data_dir / "raw" / "sentinel2").glob("*/acquisition_manifest.json")):
            manifest = _manifest(path)
            acquired = datetime.fromisoformat(manifest["acquired_at_utc"].replace("Z", "+00:00"))
            observations.append(LocalObservation(acquired, path, manifest['product_id']))
        return tuple(sorted(observations, key=lambda item: item.acquired_at))
    except Exception:
        raise LocalPreviewError("Catalogue local indisponible ou manifeste non vérifié.") from None


def _load_observation(observation):
    manifest = _manifest(observation.manifest_path)
    if datetime.fromisoformat(manifest["acquired_at_utc"].replace("Z", "+00:00")) != observation.acquired_at:
        raise ValueError("Date locale modifiée.")
    root = observation.manifest_path.parent.resolve()
    paths = {}
    for role, record in manifest["assets"].items():
        path = (root / record["path"]).resolve()
        if not path.is_relative_to(root):
            raise ValueError("Actif hors du produit local.")
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != record["sha256"]:
            raise ValueError("Empreinte locale différente du manifeste.")
        paths[role] = path
    return create_sentinel2_acquisition(
        product_id=manifest["product_id"], acquired_at=observation.acquired_at,
        b04_path=paths["b04"], b08_path=paths["b08"], scl_path=paths["scl"],
        calibration_xml_path=paths["calibration_xml"],
        asset_product_ids={role: record["product_id"] for role, record in manifest["assets"].items()},
    )


def _profile_aoi(data_dir, scientific_profile):
    root = (data_dir / "aoi").resolve()
    aoi_id = scientific_profile.aoi_id if scientific_profile is not None else "akanda"
    path = (root / f"{aoi_id}.geojson").resolve()
    if not path.is_relative_to(root):
        raise ValueError("AOI hors du répertoire local autorisé.")
    return path


def _observation_revision(observation):
    """File identity, size, mtime/ctime and manifest digest of one observation."""
    manifest = _manifest(observation.manifest_path)
    root = observation.manifest_path.parent.resolve()
    paths = [observation.manifest_path]
    for asset in manifest["assets"].values():
        path = (root / asset["path"]).resolve()
        if not path.is_relative_to(root):
            raise ValueError("Actif hors du produit local.")
        paths.append(path)
    records = []
    for path in paths:
        stat = path.stat()
        records.append((str(path.resolve()), stat.st_size, stat.st_mtime_ns,
                        stat.st_ctime_ns, stat.st_ino))
    records.append((manifest["product_id"], observation.acquired_at.isoformat(),
                    hashlib.sha256(observation.manifest_path.read_bytes()).hexdigest()))
    return records


def local_preview_key(earlier, later, data_dir, *, scl_policy=None,
                      threshold=None, scientific_profile: ScientificProfile | None = None):
    """Cheap invalidation for normal local file edits; not an integrity check.

    Full SHA-256 verification still runs on every analysis cache miss. File
    identity, size, mtime and ctime detect replacement/editing; small manifests
    and AOI are also content-hashed. No scientific results are rounded.
    """
    try:
        if scientific_profile is not None:
            if ((scl_policy is not None and scl_policy != scientific_profile.scl_policy) or
                    (threshold is not None and threshold != scientific_profile.change_threshold)):
                raise ValueError("Paramètres différents du profil.")
            scl_policy, threshold = scientific_profile.scl_policy, scientific_profile.change_threshold
        scl_policy = scl_policy or "usable_land"
        threshold = DEFAULT_CHANGE_THRESHOLD if threshold is None else threshold
        records = []
        for observation in (earlier, later):
            records.extend(_observation_revision(observation))
        aoi = _profile_aoi(data_dir, scientific_profile)
        records.append((str(aoi.resolve()), hashlib.sha256(aoi.read_bytes()).hexdigest()))
        return ("local-science-v2-profiles", scientific_profile.fingerprint if scientific_profile else None,
                scl_policy, threshold, tuple(records))
    except Exception:
        raise LocalPreviewError("Impossible de vérifier les entrées locales du cache.") from None


def analyze_local_akanda(earlier: LocalObservation, later: LocalObservation, data_dir: Path,
                         *, scientific_profile: ScientificProfile | None = None):
    """Delegate calibration, masking, harmonization and statistics to the service."""
    if earlier.acquired_at >= later.acquired_at:
        raise LocalPreviewError("Choisir deux acquisitions dans l’ordre chronologique.")
    try:
        return analyze_temporal_vegetation_change(
            _load_observation(earlier), _load_observation(later),
            aoi_path=_profile_aoi(data_dir, scientific_profile), scientific_profile=scientific_profile,
        )
    except Exception:
        raise LocalPreviewError(
            "Analyse locale impossible : vérifier les acquisitions, leurs manifestes, l’AOI et le profil."
        ) from None


def local_series_key(observations, data_dir, scientific_profile: ScientificProfile, period,
                     gap_warning_days):
    """Cheap cache invalidation for a series; full SHA-256 still runs on a miss."""
    try:
        records = []
        for observation in observations:
            records.extend(_observation_revision(observation))
        aoi = _profile_aoi(data_dir, scientific_profile)
        records.append((str(aoi.resolve()), hashlib.sha256(aoi.read_bytes()).hexdigest()))
        return ("local-ndvi-series-v1", scientific_profile.fingerprint,
                tuple(day.isoformat() for day in period), float(gap_warning_days), tuple(records))
    except Exception:
        raise LocalPreviewError("Impossible de vérifier les entrées locales du cache.") from None


def analyze_local_ndvi_series(observations, data_dir: Path, *, scientific_profile: ScientificProfile,
                              period, gap_warning_days: float = 15.0):
    """Verify every manifest and asset digest, then delegate to the series engine."""
    from src.services.ndvi_time_series import analyze_ndvi_time_series

    try:
        acquisitions, digests = [], {}
        for observation in observations:
            acquisitions.append(_load_observation(observation))
            manifest = _manifest(observation.manifest_path)
            digests[manifest["product_id"]] = {role: record["sha256"]
                                               for role, record in manifest["assets"].items()}
        return analyze_ndvi_time_series(
            acquisitions, aoi_path=_profile_aoi(data_dir, scientific_profile),
            scientific_profile=scientific_profile, period=period,
            gap_warning_days=gap_warning_days, source_sha256=digests)
    except Exception:
        raise LocalPreviewError(
            "Série NDVI impossible : vérifier les acquisitions, leurs manifestes, l’AOI, la période et le profil."
        ) from None


def local_observation_key(observation, data_dir, scientific_profile: ScientificProfile):
    """Cache key per date and AOI (file revisions, AOI digest, profile)."""
    try:
        records = list(_observation_revision(observation))
        aoi = _profile_aoi(data_dir, scientific_profile)
        records.append((str(aoi.resolve()), hashlib.sha256(aoi.read_bytes()).hexdigest()))
        return ("local-ndvi-date-v1", scientific_profile.fingerprint, tuple(records))
    except Exception:
        raise LocalPreviewError("Impossible de vérifier les entrées locales du cache.") from None


def analyze_local_observation(observation: LocalObservation, data_dir: Path, *,
                              scientific_profile: ScientificProfile):
    """Verify manifest and asset digests, then compute the NDVI of one date."""
    from src.services.ndvi_observation import analyze_ndvi_observation

    try:
        return analyze_ndvi_observation(_load_observation(observation),
                                        aoi_path=_profile_aoi(data_dir, scientific_profile),
                                        scientific_profile=scientific_profile)
    except Exception:
        raise LocalPreviewError(
            "NDVI de la date impossible : vérifier l'acquisition, son manifeste, l'AOI et le profil.") from None


def group_local_observations(observations) -> tuple[tuple[LocalObservation, ...], ...]:
    """Group products of one datatake (same relative orbit, ≤ 15 min apart); never mixes dates."""
    from src.services.ndvi_observation import ACQUISITION_WINDOW, mgrs_tile, relative_orbit

    groups = []
    for observation in sorted(observations, key=lambda item: item.acquired_at):
        current = groups[-1] if groups else None
        if (current and relative_orbit(observation.product_id) == relative_orbit(current[0].product_id)
                and observation.acquired_at - current[0].acquired_at <= ACQUISITION_WINDOW
                and mgrs_tile(observation.product_id) not in {mgrs_tile(item.product_id) for item in current}):
            current.append(observation)
        else:
            groups.append([observation])
    return tuple(tuple(group) for group in groups)


def local_group_key(group, data_dir, scientific_profile: ScientificProfile):
    """Cache key per acquisition group (all tile revisions), AOI digest and profile."""
    try:
        records = []
        for observation in group:
            records.extend(_observation_revision(observation))
        aoi = _profile_aoi(data_dir, scientific_profile)
        records.append((str(aoi.resolve()), hashlib.sha256(aoi.read_bytes()).hexdigest()))
        return ("local-ndvi-group-v1", scientific_profile.fingerprint, tuple(records))
    except Exception:
        raise LocalPreviewError("Impossible de vérifier les entrées locales du cache.") from None


def analyze_local_group(group, data_dir: Path, *, scientific_profile: ScientificProfile):
    """Verify every tile's manifest and digests, then compute the (mosaicked) NDVI of one datatake."""
    from src.services.ndvi_observation import analyze_ndvi_mosaic

    try:
        return analyze_ndvi_mosaic([_load_observation(observation) for observation in group],
                                   aoi_path=_profile_aoi(data_dir, scientific_profile),
                                   scientific_profile=scientific_profile)
    except Exception:
        raise LocalPreviewError(
            "NDVI de l'acquisition impossible : vérifier les tuiles, leurs manifestes, l'AOI et le profil.") from None
