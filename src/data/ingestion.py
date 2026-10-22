"""Acquisition et mise à jour des données géospatiales à développer."""
import logging
import hashlib
import os
import warnings
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Mapping
import xml.etree.ElementTree as ET

import boto3
from botocore.client import BaseClient
from boto3.s3.transfer import TransferConfig
from dataclasses import replace
from pystac_client import Client

from src.data.acquisition_manifest import (
    ASSET_ROLES,
    build_acquisition_manifest,
    write_acquisition_manifest,
)


from src.network_config import NetworkSettings, STAC_API_URL, NetworkConfigurationError
from src.data.network import (
    NetworkError, SecureStacIO, classify_error, create_s3_client, run_with_retry,
)
from src.data.download_control import TransferAborted
STAC_PAGE_SIZE = 100
DEFAULT_STAC_MAX_ITEMS = 1000
STAC_TIMEOUT_SECONDS = 30

logger = logging.getLogger(__name__)


class STACSearchError(NetworkError):
    """A STAC search failed or returned an incomplete response."""


class STACSearchTruncatedWarning(UserWarning):
    """The configured result cap stopped a STAC search before completion."""


class STACSearchResults(list):
    """List-compatible STAC items with explicit search completeness metadata."""

    def __init__(self, items=(), *, truncated: bool, raw_count: int):
        super().__init__(items)
        self.truncated = bool(truncated)
        self.raw_count = int(raw_count)


def create_cdse_s3_client():
    """Create an authenticated S3 client for Copernicus Data Space."""

    settings = NetworkSettings.from_environment()
    access_key = os.getenv("CDSE_S3_ACCESS_KEY")
    secret_key = os.getenv("CDSE_S3_SECRET_KEY")
    if not access_key or not secret_key:
        raise NetworkConfigurationError("Variables CDSE_S3_ACCESS_KEY et CDSE_S3_SECRET_KEY manquantes.")
    return create_s3_client(boto3, settings, access_key, secret_key)


def get_asset(item: dict, asset_name: str) -> dict:
    """Return an asset from a STAC item."""

    asset = item.get("assets", {}).get(asset_name)

    if asset is None:
        raise ValueError(f"Asset introuvable : {asset_name}")

    return asset

def get_s3_key(asset: dict, bucket_name: str = "eodata") -> str:
    """Extract the object key from an S3 asset."""

    s3_uri = asset.get("href")

    if not s3_uri:
        raise ValueError("L'asset ne contient pas de href.")

    prefix = f"s3://{bucket_name}/"

    if not s3_uri.startswith(prefix):
        raise ValueError("URI S3 inattendue.")

    return s3_uri.removeprefix(prefix)

class S3DownloadError(NetworkError):
    """Public S3 failure with a category, status and safe service code."""


def _check_s3_retry_policy(client):
    if isinstance(client, BaseClient) and client.meta.config.retries.get('total_max_attempts') != 1:
        raise NetworkConfigurationError('Utiliser un client S3 GEONAF sans retries SDK imbriqués.')


def download_asset(
    s3_client,
    asset: dict,
    destination_dir: Path,
    max_retries: int | None = None,
    retry_delay: float | None = None,
    *,
    control=None,
) -> Path:
    """Download with one retry owner; legacy max_retries counts total attempts.

    Factory clients disable SDK and transfer-level retries. Existing nonempty
    local files retain their former cache behavior; integrity checks remain in
    the ingestion service and are not changed by the transport policy.
    """
    settings = NetworkSettings.from_environment()
    if max_retries is not None:
        settings = replace(settings, max_attempts=max_retries)
    if retry_delay is not None:
        settings = replace(settings, backoff_seconds=retry_delay)
    s3_uri = asset.get('href', '')
    if not isinstance(s3_uri, str) or not s3_uri.startswith('s3://eodata/'):
        raise ValueError('Adresse S3 Copernicus invalide.')
    object_key = s3_uri.removeprefix('s3://eodata/')
    filename = PurePosixPath(object_key).name
    if (not filename or filename in ('.', '..') or object_key.endswith('/')
            or any(c in filename for c in ('\\', ':', chr(0)))
            or '?' in object_key or '#' in object_key):
        raise ValueError('Clé S3 invalide.')
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination_path = destination_dir / filename
    if destination_path.exists() and destination_path.stat().st_size > 0:
        return destination_path
    options = {}
    _check_s3_retry_policy(s3_client)
    if isinstance(s3_client, BaseClient):
        options['Config'] = TransferConfig(num_download_attempts=1, use_threads=False,
                                           preferred_transfer_client='classic')
    if control is not None:
        from src.data.download_control import atomic_download
        options['Config'] = TransferConfig(num_download_attempts=1, use_threads=False,
                                           max_concurrency=1, max_io_queue=1,
                                           io_chunksize=256 * 1024, multipart_chunksize=8 * 1024**2,
                                           preferred_transfer_client='classic')
    try:
        return run_with_retry(
            lambda: (atomic_download(s3_client, object_key, destination_path, options, control)
                     if control is not None else
                     _download_file_once(s3_client, object_key, destination_path, options)),
            settings, service='s3')
    except NetworkError as error:
        raise S3DownloadError(service='s3', category=error.category, status_code=error.status_code,
                              s3_code=error.s3_code, retry_after=error.retry_after,
                              attempts=error.attempts) from None


def _download_file_once(client, key, destination, options):
    client.download_file('eodata', key, str(destination), **options)
    return destination


def search_sentinel2(
    bbox: list[float],
    start_date: str,
    end_date: str,
    max_cloud_cover: float | None = None,
    *,
    max_items: int = DEFAULT_STAC_MAX_ITEMS,
    complete: bool = False,
    intersects: dict | None = None,
) -> list[dict]:
    """Search Sentinel-2 L2A items, following STAC pages up to ``max_items``.

    ``limit`` is the service-side page size (100); ``max_items`` is the maximum
    number of raw STAC features read, including duplicate IDs. One extra item
    is read internally to distinguish a complete result set from a truncated
    one. Duplicate IDs are removed while preserving first-seen order, so the
    returned unique-item list can be shorter than ``max_items``. A
    :class:`STACSearchTruncatedWarning` is emitted if more than ``max_items``
    features exist. The return type remains a list of item dictionaries.

    ``complete=True`` explicitly removes the total item cap and follows every
    page. The legacy capped default is unchanged. ``intersects`` replaces bbox
    with an AOI geometry supplied and validated by the catalogue service.
    """
    if (
        isinstance(max_items, bool)
        or not isinstance(max_items, int)
        or max_items < 1
    ):
        raise ValueError("max_items doit être un entier strictement positif.")
    if not isinstance(complete, bool):
        raise ValueError("complete doit être un booléen.")

    query = None
    if max_cloud_cover is not None:
        query = {"eo:cloud_cover": {"lte": max_cloud_cover}}

    search_parameters = {
        "collections": ["sentinel-2-l2a"],
        "bbox": bbox,
        "datetime": (f"{start_date}T00:00:00Z/{end_date}T23:59:59"
                     + (".999999Z" if complete else "Z")),
        "limit": STAC_PAGE_SIZE,
        "max_items": None if complete else max_items + 1,
    }
    if intersects is not None:
        search_parameters.pop("bbox")
        search_parameters["intersects"] = intersects
    if query is not None:
        search_parameters["query"] = query

    settings = NetworkSettings.from_environment()
    stac_io = SecureStacIO(settings)
    try:
        search = Client.open(
            settings.stac_endpoint,
            timeout=STAC_TIMEOUT_SECONDS,
            stac_io=stac_io,
        ).search(**search_parameters)

        items = []
        seen_ids = set()
        duplicate_count = 0
        raw_count = 0
        truncated = False

        for page in search.pages_as_dicts():
            features = page.get("features") if isinstance(page, dict) else None
            if not isinstance(features, list):
                raise STACSearchError(
                    "Réponse STAC incomplète : une page ne contient pas une liste d'items."
                )

            for feature in features:
                raw_count += 1
                if not complete and raw_count > max_items:
                    truncated = True
                    break
                if not isinstance(feature, dict):
                    raise STACSearchError(
                        "Réponse STAC incomplète : un item n'est pas un objet valide."
                    )

                item_id = feature.get("id")
                if not isinstance(item_id, str) or not item_id:
                    raise STACSearchError(
                        "Réponse STAC incomplète : un item ne possède pas d'identifiant valide."
                    )
                if item_id in seen_ids:
                    duplicate_count += 1
                    continue

                seen_ids.add(item_id)
                items.append(feature)

            if truncated:
                break

    except STACSearchError:
        raise
    except NetworkConfigurationError:
        raise
    except Exception as original:
        error = classify_error(original, 'stac')
        raise STACSearchError(service='stac', category=error.category, status_code=error.status_code,
                              retry_after=error.retry_after, attempts=error.attempts) from None
    finally:
        stac_io.session.close()

    if duplicate_count:
        logger.warning(
            "La recherche STAC a ignoré %s item(s) dupliqué(s) par identifiant.",
            duplicate_count,
        )
    if truncated:
        warnings.warn(
            f"Recherche STAC tronquée à la limite configurée de {max_items} items.",
            STACSearchTruncatedWarning,
            stacklevel=2,
        )

    return STACSearchResults(
        items,
        truncated=truncated,
        raw_count=raw_count,
    )

def select_best_product(items: list[dict]) -> dict:
    """Select the Sentinel-2 product with the lowest cloud cover."""

    if not items:
        raise ValueError("Aucun produit Sentinel-2 disponible.")

    return min(
        items,
        key=lambda item: item.get("properties", {}).get(
            "eo:cloud_cover",
            float("inf"),
        ),
    )


_MULTIHASH_ALGORITHMS = {
    0x12: ("sha256", 32),  # sha2-256
    0x16: ("sha3_256", 32),
    0xD5: ("md5", 16),
}


def _read_multihash_varint(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while offset < len(data) and shift <= 63:
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if byte < 0x80:
            return value, offset
        shift += 7
    raise ValueError("Format de checksum STAC non pris en charge pour le manifeste.")


def _stac_checksum(asset: Mapping[str, object]) -> tuple[str, str] | None:
    """Parse supported legacy SHA-256 and STAC File multihash checksums."""
    value = asset.get("file:checksum")
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("Format de checksum STAC non pris en charge pour le manifeste.")
    normalized = value.strip().lower()
    if normalized.startswith("sha256:"):
        normalized = normalized.removeprefix("sha256:")
        if len(normalized) == 64 and all(c in "0123456789abcdef" for c in normalized):
            return "sha256", normalized
        raise ValueError("Format de checksum STAC non pris en charge pour le manifeste.")

    # Keep compatibility with the previously accepted bare SHA-256 digest.
    if len(normalized) == 64 and all(c in "0123456789abcdef" for c in normalized):
        return "sha256", normalized

    if not normalized or len(normalized) % 2 or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise ValueError("Format de checksum STAC non pris en charge pour le manifeste.")
    try:
        encoded = bytes.fromhex(normalized)
        code, offset = _read_multihash_varint(encoded, 0)
        digest_length, offset = _read_multihash_varint(encoded, offset)
    except (ValueError, TypeError):
        raise ValueError("Format de checksum STAC non pris en charge pour le manifeste.") from None
    algorithm = _MULTIHASH_ALGORITHMS.get(code)
    if (
        algorithm is None
        or digest_length != algorithm[1]
        or len(encoded) - offset != digest_length
    ):
        raise ValueError("Format de checksum STAC non pris en charge pour le manifeste.")
    return algorithm[0], encoded[offset:].hex()


def _stac_sha256(asset: Mapping[str, object]) -> str | None:
    """Compatibility helper for callers that specifically require SHA-256."""
    parsed = _stac_checksum(asset)
    if parsed is None:
        return None
    algorithm, digest = parsed
    if algorithm != "sha256":
        raise ValueError("Le checksum STAC n'est pas un SHA-256.")
    return digest


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_checksum(path: Path, algorithm: str) -> str:
    digest = hashlib.new(algorithm)
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_relative_path(value: object) -> tuple[str, ...] | None:
    if not isinstance(value, str) or not value.strip():
        return None
    parsed = PurePosixPath(value.replace("\\", "/"))
    if parsed.is_absolute() or ".." in parsed.parts:
        return None
    parts = tuple(
        part.casefold()
        for part in parsed.parts
        if part not in ("", ".", "/")
    )
    if parts and parts[0].endswith(".safe"):
        parts = parts[1:]
    return parts or None


def _safe_manifest_component_checksum(
    manifest_path: Path, xml_asset: Mapping[str, object]
) -> tuple[str, str]:
    """Read the XML component digest bound by a checksum-verified SAFE manifest."""
    target_path = _safe_relative_path(xml_asset.get("file:local_path"))
    if target_path is None:
        raise ValueError("Le chemin logique STAC du XML est absent.")
    try:
        root = ET.parse(manifest_path).getroot()
    except (OSError, ET.ParseError):
        raise ValueError("Le manifeste SAFE est illisible ou invalide.") from None

    matches = []
    for data_object in root.iter():
        if data_object.tag.rsplit("}", 1)[-1].casefold() not in {
            "dataobject", "data_object"
        }:
            continue
        location = next(
            (
                node.attrib.get("href")
                for node in data_object.iter()
                if node.tag.rsplit("}", 1)[-1].casefold() == "filelocation"
            ),
            None,
        )
        checksum = next(
            (
                node
                for node in data_object.iter()
                if node.tag.rsplit("}", 1)[-1].casefold() == "checksum"
            ),
            None,
        )
        if _safe_relative_path(location) == target_path and checksum is not None:
            matches.append(
                (checksum.attrib.get("checksumName", ""), (checksum.text or "").strip())
            )

    if len(matches) != 1:
        raise ValueError("Le manifeste SAFE ne lie pas univoquement l'asset XML STAC.")
    algorithm_name, digest = matches[0]
    algorithms = {"md5": ("md5", 32), "sha3-256": ("sha3_256", 64)}
    algorithm = algorithms.get(algorithm_name.casefold())
    if (
        algorithm is None
        or len(digest) != algorithm[1]
        or any(character not in "0123456789abcdefABCDEF" for character in digest)
    ):
        raise ValueError("Checksum du XML dans le manifeste SAFE invalide ou inconnu.")
    return algorithm[0], digest.casefold()


def _download_small_stac_asset(
    s3_client, asset: Mapping[str, object], destination: Path, max_bytes: int, *, control=None
) -> Path:
    """Fetch a bounded metadata asset without permitting an oversized response."""
    _check_s3_retry_policy(s3_client)
    def fetch():
        if control is not None:
            control.check()
        response = s3_client.get_object(Bucket="eodata", Key=get_s3_key(asset))
        body = response.get("Body")
        try:
            if body is None or not callable(getattr(body, "read", None)):
                raise ValueError("Réponse metadata S3 sans flux de contenu.")
            content_length = response.get("ContentLength")
            if (
                not isinstance(content_length, int)
                or isinstance(content_length, bool)
                or content_length < 1
                or content_length > max_bytes
            ):
                raise ValueError("Taille de l'asset metadata supérieure à la limite autorisée.")
            payload = body.read(max_bytes + 1)
            if control is not None:
                control.consume(len(payload))
            if len(payload) != content_length or len(payload) > max_bytes:
                raise ValueError("Taille reçue de l'asset metadata incohérente.")
        finally:
            if body is not None:
                body.close()

        return payload

    payload = run_with_retry(fetch, NetworkSettings.from_environment(), service='s3')

    destination.parent.mkdir(parents=True, exist_ok=True)
    if control is not None:
        from src.data.download_control import atomic_metadata
        control.check()
        if len(payload) != asset.get("file:size"):
            raise TransferAborted("Taille des métadonnées différente de la déclaration STAC.")
        return atomic_metadata(destination, payload)
    with destination.open("xb") as output:
        output.write(payload)
    return destination


def ingest_sentinel2_product(
    s3_client,
    bbox: list[float],
    start_date: str,
    end_date: str,
    destination_dir: Path,
    max_cloud_cover: float | None = None,
    asset_names: list[str] | None = None,
    product_id: str | None = None,
    max_items: int = DEFAULT_STAC_MAX_ITEMS,
    manifest_asset_roles: Mapping[str, str] | None = None,
    source_integrity_mode: str = "strict",
    *,
    product_item: dict | None = None,
    control=None,
) -> dict:
    """Search, select and download a Sentinel-2 product.

    When manifest_asset_roles is supplied, it maps the required manifest
    roles (B04, B08, SCL and calibration XML) to asset keys on the selected
    STAC item. Those assets must also be requested in asset_names. A
    versioned provenance manifest is then written alongside the downloaded
    assets. ``source_integrity_mode`` defaults to strict verification; the
    exploratory mode permits a new, uncached asset when no source checksum
    can be confirmed and records that degraded status in the manifest.
    """

    if source_integrity_mode not in {"strict", "exploratory"}:
        raise ValueError("source_integrity_mode doit être 'strict' ou 'exploratory'.")

    if asset_names is None:
        asset_names = [
            "B02_10m",
            "B03_10m",
            "B04_10m",
            "B08_10m",
            "SCL_20m",
        ]

    items = [product_item] if product_item is not None else search_sentinel2(
        bbox=bbox,
        start_date=start_date,
        end_date=end_date,
        max_cloud_cover=max_cloud_cover,
        max_items=max_items,
    )

    if product_id is not None:
        product = next(
            (item for item in items if item["id"] == product_id),
            None,
        )

        if product is None:
            raise ValueError(
                f"Produit introuvable dans les résultats STAC : {product_id}"
            )
    else:
        product = select_best_product(items)

    selected_product_id = product["id"]
    product_dir = destination_dir / selected_product_id
    integrity_records = {}
    integrity_evidence = None
    expected_checksums = {}
    safe_manifest_path = None
    safe_manifest_checksum = None
    xml_integrity_fallback = False
    xml_checksum_status = None
    xml_raw_checksum = None

    if manifest_asset_roles is not None:
        if (
            not isinstance(manifest_asset_roles, Mapping)
            or set(manifest_asset_roles) != ASSET_ROLES
            or len(set(manifest_asset_roles.values())) != len(ASSET_ROLES)
            or any(
                not isinstance(asset_name, str) or not asset_name.strip()
                for asset_name in manifest_asset_roles.values()
            )
        ):
            raise ValueError(
                "La génération du manifeste exige le mapping STAC B04/B08/SCL/XML."
            )
        stac_assets = product.get("assets")
        if not isinstance(stac_assets, Mapping) or not set(
            manifest_asset_roles.values()
        ).issubset(stac_assets):
            raise ValueError(
                "Un identifiant d'asset du manifeste est absent de l'item STAC."
            )
        missing_roles = set(manifest_asset_roles.values()) - set(asset_names)
        if missing_roles:
            raise ValueError(
                "Les assets du manifeste doivent être inclus dans asset_names."
            )
        for role, asset_name in manifest_asset_roles.items():
            asset = get_asset(product, asset_name)
            raw_checksum = asset.get("file:checksum")
            try:
                expected_checksum = _stac_checksum(asset)
                checksum_status = "valid" if expected_checksum is not None else "missing"
            except ValueError:
                expected_checksum = None
                checksum_status = "invalid"
            if role == "calibration_xml":
                xml_checksum_status = checksum_status
                xml_raw_checksum = raw_checksum

            if expected_checksum is None and role == "calibration_xml":
                xml_integrity_fallback = True
                integrity_records[role] = {
                    "status": "unconfirmed",
                    "method": "none",
                    "declared_checksum_status": checksum_status,
                    "declared_checksum": raw_checksum if isinstance(raw_checksum, str) else None,
                }
            elif expected_checksum is None:
                if source_integrity_mode == "strict":
                    raise ValueError(
                        f"Checksum source absent ou invalide pour l'asset {role}."
                    )
                integrity_records[role] = {
                    "status": "unconfirmed",
                    "method": "none",
                    "declared_checksum_status": checksum_status,
                    "declared_checksum": raw_checksum if isinstance(raw_checksum, str) else None,
                }
            else:
                expected_checksums[asset_name] = expected_checksum
                integrity_records[role] = {
                    "status": "verified",
                    "method": "stac_file_checksum",
                    "algorithm": expected_checksum[0],
                    "declared_checksum_status": "valid",
                    "declared_checksum": raw_checksum,
                }

            cached_path = product_dir / Path(get_s3_key(asset)).name
            if cached_path.exists():
                if expected_checksum is None and role != "calibration_xml":
                    raise ValueError(
                        "Cache local sans checksum source vérifiable : manifeste refusé."
                    )
                if expected_checksum is not None and (
                    not cached_path.is_file()
                    or _file_checksum(cached_path, expected_checksum[0]) != expected_checksum[1]
                ):
                    raise ValueError(
                        "Le cache local ne correspond pas au checksum de l'asset STAC."
                    )

        if xml_integrity_fallback:
            safe_assets = product.get("assets", {})
            safe_asset = safe_assets.get("safe_manifest") if isinstance(safe_assets, Mapping) else None
            safe_error = None
            if not isinstance(safe_asset, Mapping):
                safe_error = "Asset SAFE manifest absent."
            else:
                try:
                    safe_manifest_checksum = _stac_checksum(safe_asset)
                    if safe_manifest_checksum is None:
                        raise ValueError("Checksum STAC du manifeste SAFE absent.")
                    safe_size = safe_asset.get("file:size")
                    if not isinstance(safe_size, int) or safe_size <= 0 or safe_size > 1024 * 1024:
                        raise ValueError("Taille du manifeste SAFE absente ou excessive.")
                    safe_manifest_path = product_dir / Path(get_s3_key(safe_asset)).name
                    if safe_manifest_path.exists() and (
                        not safe_manifest_path.is_file()
                        or _file_checksum(safe_manifest_path, safe_manifest_checksum[0])
                        != safe_manifest_checksum[1]
                    ):
                        raise ValueError("Cache local du manifeste SAFE invalide.")
                    if not safe_manifest_path.exists():
                        safe_manifest_path = _download_small_stac_asset(
                            s3_client=s3_client,
                            asset=safe_asset,
                            destination=safe_manifest_path,
                            max_bytes=1024 * 1024,
                            **({'control': control} if control is not None else {}),
                        )
                    if _file_checksum(safe_manifest_path, safe_manifest_checksum[0]) != safe_manifest_checksum[1]:
                        raise ValueError("Le manifeste SAFE ne correspond pas à son checksum STAC.")
                    xml_asset = get_asset(product, manifest_asset_roles["calibration_xml"])
                    xml_checksum = _safe_manifest_component_checksum(safe_manifest_path, xml_asset)
                    expected_checksums[manifest_asset_roles["calibration_xml"]] = xml_checksum
                    integrity_records["calibration_xml"] = {
                        "status": "verified",
                        "method": "safe_manifest_component_checksum",
                        "algorithm": xml_checksum[0],
                        "declared_checksum_status": xml_checksum_status,
                        "declared_checksum": xml_raw_checksum if isinstance(xml_raw_checksum, str) else None,
                        "evidence_asset_id": "safe_manifest",
                    }
                    integrity_evidence = {
                        "asset_id": "safe_manifest",
                        "path": safe_manifest_path.resolve(strict=True).relative_to(
                            product_dir.resolve(strict=True)
                        ).as_posix(),
                        "sha256": _file_sha256(safe_manifest_path),
                        "source_checksum_algorithm": safe_manifest_checksum[0],
                        "source_checksum": safe_asset.get("file:checksum"),
                    }
                except TransferAborted:
                    raise
                except (OSError, ValueError):
                    safe_error = "Aucune preuve de checksum alternative valide via le manifeste SAFE."

            if safe_error:
                if source_integrity_mode == "strict":
                    raise ValueError(
                        "Checksum du XML invalide et aucune preuve source alternative vérifiable."
                    ) from None
                safe_manifest_path = None
                integrity_records["calibration_xml"] = {
                    "status": "unconfirmed",
                    "method": "none",
                    "declared_checksum_status": xml_checksum_status,
                    "declared_checksum": xml_raw_checksum if isinstance(xml_raw_checksum, str) else None,
                }

            xml_asset = get_asset(product, manifest_asset_roles["calibration_xml"])
            cached_xml = product_dir / Path(get_s3_key(xml_asset)).name
            if cached_xml.exists():
                expected_xml = expected_checksums.get(manifest_asset_roles["calibration_xml"])
                if expected_xml is None:
                    raise ValueError(
                        "Cache XML sans checksum source vérifiable : manifeste refusé."
                    )
                if not cached_xml.is_file() or _file_checksum(cached_xml, expected_xml[0]) != expected_xml[1]:
                    raise ValueError("Le cache XML ne correspond pas à la preuve source.")

    downloaded_assets = {}

    requested_asset_names = list(asset_names)
    if safe_manifest_path is not None and "safe_manifest" in requested_asset_names:
        requested_asset_names.remove("safe_manifest")

    for asset_name in requested_asset_names:
        if control is not None:
            control.check()
        asset = get_asset(product, asset_name)

        local_path = download_asset(
            s3_client=s3_client,
            asset=asset,
            destination_dir=product_dir,
            **({'control': control} if control is not None else {}),
        )

        downloaded_assets[asset_name] = local_path
        if control is not None and local_path.stat().st_size != asset.get("file:size"):
            raise TransferAborted("Taille reçue différente de la taille STAC ; publication refusée.")
        if manifest_asset_roles is not None and asset_name in manifest_asset_roles.values():
            expected_checksum = expected_checksums.get(asset_name)
            if expected_checksum is None:
                if source_integrity_mode == "strict":
                    raise ValueError(f"Aucune preuve source vérifiable pour l'asset {asset_name}.")
            elif _file_checksum(local_path, expected_checksum[0]) != expected_checksum[1]:
                raise ValueError(
                    "L'asset téléchargé ne correspond pas à la preuve de checksum source."
                )

    result = {
        "product": product,
        "product_id": selected_product_id,
        "directory": product_dir,
        "assets": downloaded_assets,
    }
    if manifest_asset_roles is not None:
        role_paths = {
            role: downloaded_assets[asset_name]
            for role, asset_name in manifest_asset_roles.items()
        }
        manifest = build_acquisition_manifest(
            product=product,
            asset_paths=role_paths,
            asset_ids=manifest_asset_roles,
            stac_source=NetworkSettings.from_environment().stac_endpoint,
            ingested_at=datetime.now(timezone.utc),
            product_directory=product_dir,
            integrity={
                "mode": source_integrity_mode,
                "source_assets": integrity_records,
                "evidence": {"safe_manifest": integrity_evidence} if integrity_evidence else {},
            },
        )
        manifest_path = write_acquisition_manifest(
            manifest, product_dir / "acquisition_manifest.json"
        )
        result["manifest"] = manifest
        result["manifest_path"] = manifest_path
    return result
