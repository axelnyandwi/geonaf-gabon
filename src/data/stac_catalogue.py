"""Copernicus metadata adapter; uses the existing secured STAC transport only."""

from datetime import datetime, timezone
import json
import math
import re

from src.data import ingestion
from src.data.catalogue import CatalogueError, CatalogueRecord, CatalogueResult
from src.network_config import NetworkSettings


def _record(item):
    identifier = item.get("id")
    collection = item.get("collection", "sentinel-2-l2a")
    if (not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}", identifier)
            or collection != "sentinel-2-l2a"):
        raise CatalogueError("Identité STAC Sentinel-2 invalide.")
    properties = item.get("properties", {})
    try:
        acquired = datetime.fromisoformat(properties["datetime"].replace("Z", "+00:00"))
        if acquired.tzinfo is None or acquired.utcoffset() is None:
            raise ValueError
    except (KeyError, ValueError, TypeError, AttributeError):
        raise CatalogueError("Date UTC absente ou invalide dans le catalogue STAC.") from None
    cloud = properties.get("eo:cloud_cover")
    if (isinstance(cloud, bool) or not isinstance(cloud, (int, float))
            or not math.isfinite(cloud) or not 0 <= cloud <= 100):
        cloud = None
    assets = item.get("assets", {})
    if not isinstance(assets, dict):
        raise CatalogueError("Liste des assets STAC invalide.")

    def available(*keys):
        return any(isinstance(assets.get(key), dict) and isinstance(assets[key].get("href"), str)
                   and bool(assets[key]["href"].strip()) for key in keys)

    return CatalogueRecord(collection, identifier, acquired.astimezone(timezone.utc), cloud,
                           available("B04_10m", "B04"), available("B08_10m", "B08"),
                           available("SCL_20m", "SCL"))


class CopernicusCatalogue:
    def search(self, query):
        try:
            settings = NetworkSettings.from_environment()
            items = ingestion.search_sentinel2(
                list(query.aoi.bbox), query.start.isoformat(), query.end.isoformat(),
                max_cloud_cover=query.max_cloud_cover, complete=True,
                intersects=json.loads(query.aoi.geometry_json),
            )
            if not isinstance(items, ingestion.STACSearchResults):
                raise CatalogueError("La complétude de la recherche STAC est inconnue.")
            records = tuple(sorted((_record(item) for item in items),
                                   key=lambda row: (row.acquired_at_utc, row.stac_id)))
            return CatalogueResult(query, records, not items.truncated, items.raw_count,
                                   settings.stac_endpoint, datetime.now(timezone.utc))
        except CatalogueError:
            raise
        except Exception:
            # Transport errors are already categorized internally; never expose
            # response bodies, URLs with credentials or local TLS configuration.
            raise CatalogueError("Recherche Copernicus indisponible. Vérifier le réseau et la configuration TLS.") from None
