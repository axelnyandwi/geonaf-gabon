"""Versioned provenance manifests for future Sentinel-2 ingestions."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET

import numpy as np
import rasterio

from src.data.radiometric_calibration import read_sentinel2_calibration


ASSET_ROLES = frozenset({"b04", "b08", "scl", "calibration_xml"})
MANIFEST_SCHEMA_VERSION = "geonaf.sentinel2-acquisition/v1"


def _utc_string(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Les dates du manifeste doivent inclure un fuseau horaire.")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _parse_stac_datetime(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("La date STAC de l'acquisition est absente ou invalide.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        _utc_string(parsed)
    except (ValueError, OverflowError):
        raise ValueError("La date STAC de l'acquisition est absente ou invalide.") from None
    return parsed.astimezone(timezone.utc)


def _product_id(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Identifiant produit STAC absent.")
    return value.strip().removesuffix(".SAFE")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_source(source: str) -> str:
    if not isinstance(source, str):
        raise ValueError("La source STAC doit être une URL HTTPS sans paramètres sensibles.")
    parsed = urlsplit(source)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("La source STAC doit être une URL HTTPS sans paramètres sensibles.")
    return source.rstrip("/")


def _calibration_product_id(path: Path) -> str | None:
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError):
        raise ValueError("Lecture des métadonnées XML impossible.") from None
    values = [
        node.text.strip().removesuffix(".SAFE")
        for node in root.iter()
        if node.tag.rsplit("}", 1)[-1].casefold() in {"product_uri", "product_id"}
        and node.text
        and node.text.strip()
    ]
    if len(set(values)) > 1:
        raise ValueError("Identifiants produits contradictoires dans le XML de calibration.")
    return values[0] if values else None


def build_acquisition_manifest(
    *,
    product: Mapping[str, object],
    asset_paths: Mapping[str, str | Path],
    asset_ids: Mapping[str, str],
    stac_source: str,
    ingested_at: datetime,
    product_directory: str | Path,
    integrity: Mapping[str, object] | None = None,
) -> dict:
    """Build a JSON-safe manifest from one selected STAC item and its assets.

    The caller must pass the STAC asset key for each role. Paths are stored
    relative to the product directory; absolute machine paths are never saved.
    """
    if not isinstance(product, Mapping):
        raise ValueError("L'item STAC doit être un objet valide.")
    if not isinstance(asset_paths, Mapping) or set(asset_paths) != ASSET_ROLES:
        raise ValueError("Le manifeste exige les quatre rôles B04, B08, SCL et XML.")
    if not isinstance(asset_ids, Mapping) or set(asset_ids) != ASSET_ROLES:
        raise ValueError("Les identifiants STAC doivent couvrir les quatre rôles requis.")
    if any(not isinstance(value, str) or not value.strip() for value in asset_ids.values()):
        raise ValueError("Chaque actif doit conserver son identifiant STAC.")
    if len(set(asset_ids.values())) != len(ASSET_ROLES):
        raise ValueError("Chaque rôle doit pointer vers un asset STAC distinct.")
    stac_assets = product.get("assets")
    if not isinstance(stac_assets, Mapping) or not set(asset_ids.values()).issubset(
        stac_assets
    ):
        raise ValueError("Un identifiant d'asset ne provient pas de l'item STAC fourni.")

    product_id = _product_id(product.get("id"))
    collection = product.get("collection")
    if not isinstance(collection, str) or not collection.strip():
        raise ValueError("La collection STAC de l'acquisition est absente.")
    properties = product.get("properties")
    acquired_at = _parse_stac_datetime(
        properties.get("datetime") if isinstance(properties, Mapping) else None
    )
    ingested_at_utc = _utc_string(ingested_at)
    source = _validate_source(stac_source)

    root = Path(product_directory).resolve(strict=True)
    asset_records = {}
    resolved_paths = {}
    for role in sorted(ASSET_ROLES):
        path = Path(asset_paths[role]).resolve(strict=True)
        try:
            relative_path = path.relative_to(root)
        except ValueError:
            raise ValueError("Chaque asset doit avoir un chemin relatif au dossier produit.") from None
        if not path.is_file() or path.stat().st_size <= 0:
            raise ValueError("Un asset du manifeste est absent ou vide.")
        resolved_paths[role] = path
        record = {
            "id": asset_ids[role].strip(),
            "product_id": product_id,
            "path": relative_path.as_posix(),
            "sha256": _sha256(path),
        }
        source_asset = stac_assets[asset_ids[role]]
        if not isinstance(source_asset, Mapping) or not isinstance(
            source_asset.get("href"), str
        ) or not source_asset["href"].strip():
            raise ValueError("La référence d'asset STAC est absente ou invalide.")
        if role != "calibration_xml":
            try:
                with rasterio.open(path) as dataset:
                    if dataset.crs is None or dataset.count != 1:
                        raise ValueError("CRS absent ou raster non mono-bande dans un asset.")
                    transform = dataset.transform
                    resolution = [abs(float(transform.a)), abs(float(transform.e))]
                    if not all(np.isfinite(value) and value > 0 for value in resolution):
                        raise ValueError("Résolution invalide dans un asset raster.")
                    bounds = dataset.bounds
                    record.update(
                        {
                            "crs": dataset.crs.to_string(),
                            "resolution": resolution,
                            "transform": [float(value) for value in transform[:6]],
                            "extent": [
                                float(bounds.left),
                                float(bounds.bottom),
                                float(bounds.right),
                                float(bounds.top),
                            ],
                            "nodata": dataset.nodata,
                            "width": int(dataset.width),
                            "height": int(dataset.height),
                        }
                    )
            except (OSError, rasterio.errors.RasterioError):
                raise ValueError("Lecture des métadonnées raster impossible.") from None
        asset_records[role] = record

    b04_grid = asset_records["b04"]
    b08_grid = asset_records["b08"]
    if any(
        b04_grid[key] != b08_grid[key]
        for key in ("crs", "resolution", "transform", "width", "height")
    ):
        raise ValueError("B04 et B08 ne partagent pas la même grille.")

    calibration_path = resolved_paths["calibration_xml"]
    embedded_id = _calibration_product_id(calibration_path)
    if embedded_id is not None and embedded_id != product_id:
        raise ValueError("Le XML de calibration ne correspond pas à l'item STAC.")
    try:
        calibration = read_sentinel2_calibration(calibration_path)
        calibration_record = {
            "method": "BOA",
            "xml_asset_role": "calibration_xml",
            "quantification_value": float(calibration["quantification_value"]),
            "offsets": {
                "B04": float(calibration["offsets"]["B04"]),
                "B08": float(calibration["offsets"]["B08"]),
            },
        }
    except (KeyError, TypeError, ValueError, FileNotFoundError, OverflowError):
        raise ValueError("Calibration BOA B04/B08 absente ou invalide.") from None
    if (
        not math.isfinite(calibration_record["quantification_value"])
        or calibration_record["quantification_value"] <= 0
        or any(not math.isfinite(value) for value in calibration_record["offsets"].values())
    ):
        raise ValueError("Calibration BOA B04/B08 absente ou invalide.")

    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "product_id": product_id,
        "acquired_at_utc": _utc_string(acquired_at),
        "collection": collection.strip(),
        "stac": {"source": source, "item_id": product_id},
        "assets": asset_records,
        "calibration": calibration_record,
        "ingested_at_utc": ingested_at_utc,
    }
    if integrity is not None:
        manifest["integrity"] = dict(integrity)
    validate_acquisition_manifest(manifest)
    return manifest


def validate_acquisition_manifest(manifest: Mapping[str, object]) -> None:
    """Validate the durable, JSON-safe shape before writing or consuming it."""
    if not isinstance(manifest, Mapping) or manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise ValueError("Version de schéma de manifeste non prise en charge.")
    product_id = _product_id(manifest.get("product_id"))
    collection = manifest.get("collection")
    if not isinstance(collection, str) or not collection.strip():
        raise ValueError("La collection STAC de l'acquisition est absente.")
    _parse_stac_datetime(manifest.get("acquired_at_utc"))
    _parse_stac_datetime(manifest.get("ingested_at_utc"))
    stac = manifest.get("stac")
    if not isinstance(stac, Mapping) or _product_id(stac.get("item_id")) != product_id:
        raise ValueError("L'identité STAC du manifeste ne correspond pas au produit.")
    _validate_source(stac.get("source"))

    assets = manifest.get("assets")
    if not isinstance(assets, Mapping) or set(assets) != ASSET_ROLES:
        raise ValueError("Le manifeste doit décrire exactement les quatre rôles d'assets.")
    asset_ids = []
    for role, record in assets.items():
        if not isinstance(record, Mapping):
            raise ValueError("Métadonnées d'asset invalides dans le manifeste.")
        if record.get("product_id") != product_id:
            raise ValueError("Un asset du manifeste porte un identifiant produit différent.")
        if not isinstance(record.get("id"), str) or not record["id"].strip():
            raise ValueError("Identifiant STAC d'asset absent.")
        asset_ids.append(record["id"])
        relative_path = record.get("path")
        if (
            not isinstance(relative_path, str)
            or not relative_path
            or Path(relative_path).is_absolute()
            or ".." in Path(relative_path).parts
        ):
            raise ValueError("Le manifeste contient un chemin d'asset non relatif.")
        digest = record.get("sha256")
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise ValueError("Empreinte SHA-256 d'asset invalide.")
        if role != "calibration_xml":
            resolution = record.get("resolution")
            extent = record.get("extent")
            if (
                not isinstance(record.get("crs"), str)
                or not record["crs"]
                or not isinstance(resolution, list)
                or len(resolution) != 2
                or any(not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0 for value in resolution)
                or not isinstance(extent, list)
                or len(extent) != 4
                or any(not isinstance(value, (int, float)) or not math.isfinite(value) for value in extent)
            ):
                raise ValueError("Métadonnées spatiales d'asset invalides.")
    if len(set(asset_ids)) != len(ASSET_ROLES):
        raise ValueError("Chaque rôle doit pointer vers un asset STAC distinct.")
    integrity = manifest.get("integrity")
    if integrity is not None:
        if not isinstance(integrity, Mapping) or integrity.get("mode") not in {
            "strict", "exploratory"
        }:
            raise ValueError("Politique d'intégrité du manifeste invalide.")
        source_assets = integrity.get("source_assets")
        if not isinstance(source_assets, Mapping) or set(source_assets) != ASSET_ROLES:
            raise ValueError("L'intégrité source doit couvrir les quatre assets.")
        has_unconfirmed = False
        for role, status in source_assets.items():
            if not isinstance(status, Mapping):
                raise ValueError("Statut d'intégrité source invalide.")
            state = status.get("status")
            method = status.get("method")
            checksum_state = status.get("declared_checksum_status")
            if state not in {"verified", "unconfirmed"} or checksum_state not in {
                "valid", "invalid", "missing"
            }:
                raise ValueError("Statut d'intégrité source invalide.")
            if state == "unconfirmed":
                has_unconfirmed = True
                if method != "none" or integrity["mode"] != "exploratory":
                    raise ValueError("Intégrité source non confirmée hors mode exploratoire.")
            elif method == "stac_file_checksum":
                if checksum_state != "valid" or status.get("algorithm") not in {
                    "sha256", "sha3_256", "md5"
                }:
                    raise ValueError("Checksum STAC marqué vérifié sans format valide.")
            elif method == "safe_manifest_component_checksum":
                if role != "calibration_xml" or status.get("algorithm") not in {
                    "sha256", "sha3_256", "md5"
                }:
                    raise ValueError("Preuve de checksum SAFE invalide.")
                if status.get("evidence_asset_id") != "safe_manifest":
                    raise ValueError("La preuve SAFE ne référence pas son asset source.")
            else:
                raise ValueError("Méthode d'intégrité source inconnue.")
            declared = status.get("declared_checksum")
            if declared is not None and not isinstance(declared, str):
                raise ValueError("Checksum source déclaré invalide.")
        if integrity["mode"] == "strict" and has_unconfirmed:
            raise ValueError("Le mode strict interdit une intégrité source non confirmée.")

        evidence = integrity.get("evidence")
        if not isinstance(evidence, Mapping):
            raise ValueError("Preuves d'intégrité du manifeste invalides.")
        safe_evidence = evidence.get("safe_manifest")
        uses_safe_manifest = any(
            isinstance(status, Mapping)
            and status.get("method") == "safe_manifest_component_checksum"
            for status in source_assets.values()
        )
        if uses_safe_manifest and not isinstance(safe_evidence, Mapping):
            raise ValueError("La preuve SAFE référencée est absente du manifeste.")
        if safe_evidence is not None:
            evidence_path = safe_evidence.get("path")
            evidence_hash = safe_evidence.get("sha256")
            evidence_checksum = safe_evidence.get("source_checksum")
            if (
                safe_evidence.get("asset_id") != "safe_manifest"
                or not isinstance(evidence_path, str)
                or not evidence_path
                or Path(evidence_path).is_absolute()
                or ".." in Path(evidence_path).parts
                or not isinstance(evidence_hash, str)
                or len(evidence_hash) != 64
                or any(c not in "0123456789abcdef" for c in evidence_hash)
                or safe_evidence.get("source_checksum_algorithm") not in {"md5", "sha3_256", "sha256"}
                or not isinstance(evidence_checksum, str)
            ):
                raise ValueError("Métadonnées d'empreinte du manifeste SAFE invalides.")
    calibration = manifest.get("calibration")
    if (
        not isinstance(calibration, Mapping)
        or calibration.get("method") != "BOA"
        or calibration.get("xml_asset_role") != "calibration_xml"
        or not isinstance(calibration.get("quantification_value"), (int, float))
        or not math.isfinite(calibration["quantification_value"])
        or calibration["quantification_value"] <= 0
        or not isinstance(calibration.get("offsets"), Mapping)
        or set(calibration["offsets"]) != {"B04", "B08"}
        or any(
            not isinstance(value, (int, float)) or not math.isfinite(value)
            for value in calibration["offsets"].values()
        )
    ):
        raise ValueError("Calibration BOA du manifeste invalide.")


def write_acquisition_manifest(manifest: Mapping[str, object], destination: str | Path) -> Path:
    """Persist a manifest without replacing a previous provenance record."""
    validate_acquisition_manifest(manifest)
    path = Path(destination)
    payload = json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    with path.open("x", encoding="utf-8", newline="\n") as output:
        output.write(payload)
    return path
