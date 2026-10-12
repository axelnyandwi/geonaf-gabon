"""Acquisition et mise à jour des données géospatiales à développer."""
import logging
import os
import time
import warnings
from pathlib import Path

import boto3
from botocore.exceptions import ClientError
from dotenv import load_dotenv
from pystac_client import Client


STAC_API_URL = "https://stac.dataspace.copernicus.eu/v1"
STAC_PAGE_SIZE = 100
DEFAULT_STAC_MAX_ITEMS = 1000
STAC_TIMEOUT_SECONDS = 30

logger = logging.getLogger(__name__)


class STACSearchError(RuntimeError):
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

    load_dotenv()

    access_key = os.getenv("CDSE_S3_ACCESS_KEY")
    secret_key = os.getenv("CDSE_S3_SECRET_KEY")

    if not access_key or not secret_key:
        raise ValueError(
            "Variables CDSE_S3_ACCESS_KEY et CDSE_S3_SECRET_KEY manquantes."
        )

    return boto3.client(
        "s3",
        endpoint_url="https://eodata.dataspace.copernicus.eu",
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name="default",
    )

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
        raise ValueError(f"URI S3 inattendue : {s3_uri}")

    return s3_uri.removeprefix(prefix)

def download_asset(
    s3_client,
    asset: dict,
    destination_dir: Path,
    max_retries: int = 5,
    retry_delay: int = 5,
) -> Path:
    """Download an S3 asset with local cache and retry handling."""

    s3_uri = asset["href"]

    if not s3_uri.startswith("s3://"):
        raise ValueError(
            f"Adresse S3 invalide : {s3_uri}"
        )

    # Exemple :
    # s3://eodata/Sentinel-2/.../B04_10m.jp2
    s3_path = s3_uri.removeprefix("s3://")

    bucket_name, object_key = s3_path.split("/", 1)

    filename = Path(object_key).name

    destination_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    destination_path = (
        destination_dir
        / filename
    )

    # Cache local
    if (
        destination_path.exists()
        and destination_path.stat().st_size > 0
    ):
        print(
            f"Déjà présent : {filename}"
        )
        return destination_path

    # Téléchargement avec retries
    for attempt in range(
        1,
        max_retries + 1,
    ):
        try:
            print(
                f"Téléchargement : {filename} "
                f"(tentative {attempt}/{max_retries})"
            )

            s3_client.download_file(
                bucket_name,
                object_key,
                str(destination_path),
            )

            return destination_path

        except ClientError as error:

            response = error.response

            status_code = (
                response
                .get("ResponseMetadata", {})
                .get("HTTPStatusCode")
            )

            # Erreurs serveur temporaires
            if (
                status_code
                and status_code >= 500
                and attempt < max_retries
            ):
                wait_seconds = (
                    retry_delay * attempt
                )

                print(
                    f"Erreur serveur {status_code}. "
                    f"Nouvelle tentative dans "
                    f"{wait_seconds} s..."
                )

                time.sleep(wait_seconds)

                continue

            raise

    raise RuntimeError(
        f"Impossible de télécharger : {s3_uri}"
    )


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

    try:
        search = Client.open(
            STAC_API_URL,
            timeout=STAC_TIMEOUT_SECONDS,
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
    except Exception as error:
        status_code = getattr(
            getattr(error, "response", None),
            "status_code",
            None,
        )
        status = f", HTTP {status_code}" if status_code is not None else ""
        raise STACSearchError(
            "Recherche STAC interrompue "
            f"({type(error).__name__}{status}); aucun résultat partiel n'est retourné."
        ) from None

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
