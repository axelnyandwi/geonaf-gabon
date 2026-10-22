"""Authorised, bounded retrieval of external layers for one AOI (engine, no UI).

Called only on an explicit user action. Each function reuses stored data when
possible (cache by AOI, version/month or query) and reports network bytes.
Ceilings are explicit parameters with conservative defaults.
"""

from __future__ import annotations

from datetime import date
import calendar
from pathlib import Path

from src.services.external_layers import open_store

WORLDCOVER_MAX_BYTES = 4 * 1024 * 1024
OSM_MAX_BYTES = 32 * 1024 * 1024
SENTINEL1_MAX_BYTES = 16 * 1024 * 1024  # Per polarization window (header + tiles).


def _external_client(client):
    if client is not None:
        return client
    from src.data.external_http import ExternalHttpClient
    return ExternalHttpClient()


def retrieve_worldcover(data_dir: Path, *, aoi_id: str, wgs84_bounds, client=None,
                        max_bytes: int = WORLDCOVER_MAX_BYTES) -> dict:
    from src.data.worldcover import fetch_worldcover_window

    store, repository = open_store(data_dir)
    lazy = _LazyClient(client)
    _, descriptor, report = fetch_worldcover_window(lazy, store, repository, aoi_id=aoi_id,
                                                    wgs84_bounds=wgs84_bounds, max_bytes=max_bytes)
    return {**report, "product": descriptor.product}


def retrieve_osm(data_dir: Path, *, wgs84_bounds, force_refresh: bool = False, client=None,
                 max_bytes: int = OSM_MAX_BYTES) -> dict:
    from src.data.openstreetmap import fetch_osm_snapshot

    store, repository = open_store(data_dir)
    frame, descriptor, report = fetch_osm_snapshot(_LazyClient(client), store, repository,
                                                   wgs84_bounds=wgs84_bounds, force_refresh=force_refresh,
                                                   max_bytes=max_bytes)
    return {**report, "features": len(frame), "osm_base_utc": descriptor.provenance["osm_base_utc"]}


def retrieve_sentinel1(data_dir: Path, *, aoi_id: str, wgs84_bounds, month: date, stac_io=None, s3_client=None,
                       max_bytes: int = SENTINEL1_MAX_BYTES) -> dict:
    """Search the month's IW mosaic, estimate each window from the COG header, fetch only under the cap."""
    from src.data.sentinel1 import estimate_window_bytes, fetch_sentinel1_window, search_mosaics

    store, repository = open_store(data_dir)
    if stac_io is None:
        from src.data.network import SecureStacIO
        from src.network_config import NetworkSettings
        stac_io = SecureStacIO(NetworkSettings.from_environment())
    last_day = calendar.monthrange(month.year, month.month)[1]
    mosaics, rejected, truncated = search_mosaics(stac_io, wgs84_bounds, date(month.year, month.month, 1),
                                                  date(month.year, month.month, last_day))
    report = {"items": [m.item_id for m in mosaics], "rejected_items": rejected, "truncated": truncated,
              "polarizations": {}}
    if not mosaics:
        report["status"] = "Aucune mosaïque Sentinel-1 IW publiée pour ce mois et cette AOI."
        return report
    if len(mosaics) > 1:
        report["status"] = "Plusieurs tuiles MGRS couvrent l'AOI : non pris en charge en V1."
        return report
    mosaic = mosaics[0]
    if s3_client is None:
        from src.data.ingestion import create_cdse_s3_client
        s3_client = create_cdse_s3_client()
    for polarization in ("VV", "VH"):
        key = f"sentinel1/{mosaic.item_id}_{polarization}_{aoi_id}.tif"
        entry = {"asset_size_bytes": mosaic.assets[polarization].size_bytes, "cap_bytes": max_bytes}
        if not store.exists(key):
            estimate, header = estimate_window_bytes(s3_client, mosaic, polarization, wgs84_bounds)
            entry.update(estimated_tile_bytes=estimate.compressed_bytes, header_bytes=header,
                         window=f"{estimate.width}×{estimate.height}")
            if estimate.compressed_bytes + 2 * header > max_bytes:
                entry["status"] = "refusé : estimation supérieure au plafond"
                report["polarizations"][polarization] = entry
                continue
        _, descriptor, fetched = fetch_sentinel1_window(s3_client, store, repository, mosaic, polarization,
                                                        aoi_id=aoi_id, wgs84_bounds=wgs84_bounds, max_bytes=max_bytes)
        entry.update(fetched, status="ok")
        report["polarizations"][polarization] = entry
    report["status"] = "ok"
    return report


class _LazyClient:
    """Creates the TLS client only if a cache miss actually needs the network."""

    def __init__(self, client):
        self._client = client

    def __getattr__(self, name):
        if self._client is None:
            self._client = _external_client(None)
        return getattr(self._client, name)
