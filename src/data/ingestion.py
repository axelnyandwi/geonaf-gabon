"""Acquisition et mise à jour des données géospatiales à développer."""
import logging
import os
import warnings
from pathlib import Path, PurePosixPath

import boto3
from botocore.client import BaseClient
from boto3.s3.transfer import TransferConfig
from dataclasses import replace
from pystac_client import Client

from src.network_config import NetworkSettings, STAC_API_URL, NetworkConfigurationError
from src.data.network import (
    NetworkError, SecureStacIO, classify_error, create_s3_client, run_with_retry,
)
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
    try:
        return run_with_retry(
            lambda: _download_file_once(s3_client, object_key, destination_path, options),
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
) -> list[dict]:
    """Search Sentinel-2 L2A items, following STAC pages up to ``max_items``.

    ``limit`` is the service-side page size (100); ``max_items`` is the maximum
    number of raw STAC features read, including duplicate IDs. One extra item
    is read internally to distinguish a complete result set from a truncated
    one. Duplicate IDs are removed while preserving first-seen order, so the
    returned unique-item list can be shorter than ``max_items``. A
    :class:`STACSearchTruncatedWarning` is emitted if more than ``max_items``
    features exist. The return type remains a list of item dictionaries.
    """
    if (
        isinstance(max_items, bool)
        or not isinstance(max_items, int)
        or max_items < 1
    ):
        raise ValueError("max_items doit être un entier strictement positif.")

    query = None
    if max_cloud_cover is not None:
        query = {"eo:cloud_cover": {"lte": max_cloud_cover}}

    search_parameters = {
        "collections": ["sentinel-2-l2a"],
        "bbox": bbox,
        "datetime": f"{start_date}T00:00:00Z/{end_date}T23:59:59Z",
        "limit": STAC_PAGE_SIZE,
        "max_items": max_items + 1,
    }
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
                if raw_count > max_items:
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


def _download_small_stac_asset(
    s3_client, asset: dict, destination: Path, max_bytes: int
) -> Path:
    """Fetch a bounded metadata asset without permitting an oversized response."""
    _check_s3_retry_policy(s3_client)
    def fetch():
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
            if len(payload) != content_length or len(payload) > max_bytes:
                raise ValueError("Taille reçue de l'asset metadata incohérente.")
        finally:
            if body is not None:
                body.close()

        return payload

    payload = run_with_retry(fetch, NetworkSettings.from_environment(), service='s3')

    destination.parent.mkdir(parents=True, exist_ok=True)
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
) -> dict:
    """Search, select and download a Sentinel-2 product."""

    if asset_names is None:
        asset_names = [
            "B02_10m",
            "B03_10m",
            "B04_10m",
            "B08_10m",
            "SCL_20m",
        ]

    items = search_sentinel2(
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

    downloaded_assets = {}

    for asset_name in asset_names:
        asset = get_asset(product, asset_name)

        local_path = download_asset(
            s3_client=s3_client,
            asset=asset,
            destination_dir=product_dir,
        )

        downloaded_assets[asset_name] = local_path

    return {
        "product": product,
        "product_id": selected_product_id,
        "directory": product_dir,
        "assets": downloaded_assets,
    }
