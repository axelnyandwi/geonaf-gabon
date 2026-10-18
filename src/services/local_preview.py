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


class LocalPreviewError(ValueError):
    """Public error without source paths or underlying exception details."""


@dataclass(frozen=True)
class LocalObservation:
    acquired_at: datetime
    manifest_path: Path


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
    """Only expose the two dates covered by this prototype's local study."""
    try:
        observations = []
        for path in sorted((data_dir / "raw" / "sentinel2").glob("*/acquisition_manifest.json")):
            manifest = _manifest(path)
            acquired = datetime.fromisoformat(manifest["acquired_at_utc"].replace("Z", "+00:00"))
            if acquired.date().isoformat() in {"2025-01-15", "2025-01-25"}:
                observations.append(LocalObservation(acquired, path))
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


def local_preview_key(earlier, later, data_dir, *, scl_policy="usable_land",
                      threshold=DEFAULT_CHANGE_THRESHOLD):
    """Cheap invalidation for normal local file edits; not an integrity check.

    Full SHA-256 verification still runs on every analysis cache miss. File
    identity, size, mtime and ctime detect replacement/editing; small manifests
    and AOI are also content-hashed. No scientific results are rounded.
    """
    try:
        records = []
        for observation in (earlier, later):
            manifest = _manifest(observation.manifest_path)
            root = observation.manifest_path.parent.resolve()
            paths = [observation.manifest_path]
            for asset in manifest["assets"].values():
                path = (root / asset["path"]).resolve()
                if not path.is_relative_to(root):
                    raise ValueError("Actif hors du produit local.")
                paths.append(path)
            for path in paths:
                stat = path.stat()
                records.append((str(path.resolve()), stat.st_size, stat.st_mtime_ns,
                                stat.st_ctime_ns, stat.st_ino))
            records.append((manifest["product_id"], observation.acquired_at.isoformat(),
                            hashlib.sha256(observation.manifest_path.read_bytes()).hexdigest()))
        aoi = data_dir / "aoi" / "akanda.geojson"
        records.append((str(aoi.resolve()), hashlib.sha256(aoi.read_bytes()).hexdigest()))
        return ("akanda-science-v1", scl_policy, threshold, tuple(records))
    except Exception:
        raise LocalPreviewError("Impossible de vérifier les entrées locales du cache.") from None


def analyze_local_akanda(earlier: LocalObservation, later: LocalObservation, data_dir: Path):
    """Delegate calibration, masking, harmonization and statistics to the service."""
    if earlier.acquired_at >= later.acquired_at:
        raise LocalPreviewError("Choisir deux acquisitions dans l’ordre chronologique.")
    try:
        return analyze_temporal_vegetation_change(
            _load_observation(earlier), _load_observation(later),
            aoi_path=data_dir / "aoi" / "akanda.geojson", scl_policy="usable_land",
        )
    except Exception:
        raise LocalPreviewError(
            "Analyse locale impossible : vérifier les acquisitions, leurs manifestes et l’AOI Akanda."
        ) from None
