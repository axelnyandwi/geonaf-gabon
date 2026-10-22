"""ESA WorldCover adapter: tile planning and local description, no hidden I/O.

Tiles are 3° × 3° Cloud Optimized GeoTIFFs in EPSG:4326 (1/12 000°, ≈10 m),
named after their south-west corner. Planning never downloads. The
authorised retrieval (:func:`fetch_worldcover_window`) reads only the COG
window over the AOI by HTTP Range with an exact byte estimate and a ceiling,
stores it once (never overwritten) and records its descriptor.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import math
from pathlib import Path

import rasterio
from rasterio.warp import transform_bounds

from src.data.external_sources import (
    ExternalLayerDescriptor, LayerLicense, LayerQuality, RetrievalPlan, SpatialExtent, TemporalValidity,
)

BUCKET_URL = "https://esa-worldcover.s3.eu-central-1.amazonaws.com"
VERSIONS = {"v100": 2020, "v200": 2021}
CLASSES = {
    10: "Couvert arboré", 20: "Arbustes", 30: "Prairies", 40: "Cultures", 50: "Bâti",
    60: "Sol nu / végétation clairsemée", 70: "Neige et glace", 80: "Eaux permanentes",
    90: "Zones humides herbacées", 95: "Mangroves", 100: "Mousses et lichens",
}
LICENSE = LayerLicense(
    "CC-BY-4.0",
    "© ESA WorldCover project / Contains modified Copernicus Sentinel data processed by ESA WorldCover consortium",
    "https://creativecommons.org/licenses/by/4.0/", share_alike=False)
MAX_PLANNED_TILES = 16


def _tile_name(lat0: int, lon0: int) -> str:
    return f"{'N' if lat0 >= 0 else 'S'}{abs(lat0):02d}{'E' if lon0 >= 0 else 'W'}{abs(lon0):03d}"


class WorldCoverAdapter:
    source_id = "esa-worldcover"

    def __init__(self, version: str = "v200"):
        if version not in VERSIONS:
            raise ValueError("Version WorldCover inconnue.")
        self.version, self.year = version, VERSIONS[version]

    def tiles(self, wgs84_bounds) -> tuple[str, ...]:
        left, bottom, right, top = wgs84_bounds
        if not (-180 <= left < right <= 180 and -90 <= bottom < top <= 90):
            raise ValueError("Emprise WGS84 invalide.")
        # Upper/right edges on a 3° line belong to the previous tile.
        lats = range(math.floor(bottom / 3) * 3, math.ceil(top / 3) * 3, 3)
        lons = range(math.floor(left / 3) * 3, math.ceil(right / 3) * 3, 3)
        names = tuple(_tile_name(lat, lon) for lat in lats for lon in lons)
        if len(names) > MAX_PLANNED_TILES:
            raise ValueError("Emprise trop grande pour une planification bornée.")
        return names

    def tile_url(self, name: str) -> str:
        return (f"{BUCKET_URL}/{self.version}/{self.year}/map/"
                f"ESA_WorldCover_10m_{self.year}_{self.version}_{name}_Map.tif")

    def plan(self, wgs84_bounds) -> RetrievalPlan:
        return RetrievalPlan(
            self.source_id, tuple(self.tile_url(name) for name in self.tiles(wgs84_bounds)), None,
            ("Lecture fenêtrée COG sur l'AOI recommandée (HTTP Range) ; ne pas télécharger les tuiles entières.",
             "Hôte hors de la politique réseau Copernicus actuelle : autorisation explicite requise."))

    def describe_local(self, path, *, retrieved_at: datetime, source_url: str | None = None,
                       extra_provenance: dict | None = None) -> ExternalLayerDescriptor:
        path = Path(path)
        with rasterio.open(path) as source:
            if source.crs is None or source.count != 1 or source.dtypes[0] != "uint8":
                raise ValueError("Raster WorldCover attendu : une bande uint8 avec CRS.")
            crs = source.crs.to_string()
            bounds = tuple(source.bounds)
            wgs84 = bounds if source.crs.to_epsg() == 4326 else transform_bounds(source.crs, "EPSG:4326", *bounds)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        provenance = {"retrieved_at_utc": retrieved_at.astimezone(timezone.utc).isoformat(),
                      "product_url": "https://esa-worldcover.org"}
        if source_url:
            if not source_url.startswith(BUCKET_URL + "/"):
                raise ValueError("URL source WorldCover non reconnue.")
            provenance["source_url"] = source_url
        provenance.update({key: str(value) for key, value in (extra_provenance or {}).items()})
        return ExternalLayerDescriptor(
            source_id=self.source_id, product=f"ESA WorldCover 10 m {self.year}", version=self.version,
            kind="categorical_raster",
            temporal=TemporalValidity("interval", datetime(self.year, 1, 1, tzinfo=timezone.utc),
                                      datetime(self.year, 12, 31, 23, 59, 59, tzinfo=timezone.utc)),
            resolution_m=10.0, crs=crs, extent=SpatialExtent(crs, bounds, tuple(wgs84)), license=LICENSE,
            provenance=provenance,
            quality=LayerQuality("declared", (
                "Exactitude déclarée par le producteur, non vérifiée sur l'AOI.",
                f"Carte annuelle {self.year} : ne décrit pas l'état à la date d'une acquisition ultérieure."),
                digest),
            classes=CLASSES)


def _covers(path: Path, wgs84_bounds) -> bool:
    with rasterio.open(path) as source:
        b = source.bounds if source.crs.to_epsg() == 4326 else transform_bounds(source.crs, "EPSG:4326", *source.bounds)
    tolerance = 1 / 12000  # One WorldCover cell.
    left, bottom, right, top = wgs84_bounds
    return b[0] <= left + tolerance and b[1] <= bottom + tolerance and b[2] >= right - tolerance and b[3] >= top - tolerance


def tile_bounds(name: str) -> tuple[float, float, float, float]:
    """WGS84 bounds of a 3° tile named after its south-west corner (e.g. N00E009)."""
    lat = int(name[1:3]) * (1 if name[0] == "N" else -1)
    lon = int(name[4:7]) * (1 if name[3] == "E" else -1)
    return float(lon), float(lat), float(lon + 3), float(lat + 3)


def fetch_worldcover_windows(client, store, repository, *, aoi_id: str, wgs84_bounds, version: str = "v200",
                             max_bytes: int = 4 * 1024 * 1024, now: datetime | None = None):
    """Discover every tile intersecting the AOI and return [(tile, path, descriptor, report)].

    Each tile contributes only its part of the AOI (AOI ∩ tile), with its own
    window, file, checksum and provenance; ``max_bytes`` applies per tile.
    """
    adapter = WorldCoverAdapter(version)
    left, bottom, right, top = wgs84_bounds
    output = []
    for tile in adapter.tiles(wgs84_bounds):
        tl, tb, tr, tt = tile_bounds(tile)
        part = (max(left, tl), max(bottom, tb), min(right, tr), min(top, tt))
        if part[0] >= part[2] or part[1] >= part[3]:
            continue
        output.append((tile, *_fetch_tile_window(client, store, repository, adapter, tile, aoi_id=aoi_id,
                                                  wgs84_bounds=part, max_bytes=max_bytes, now=now)))
    return output


def fetch_worldcover_window(client, store, repository, *, aoi_id: str, wgs84_bounds, version: str = "v200",
                            max_bytes: int = 4 * 1024 * 1024, now: datetime | None = None):
    """Single-tile contract: (path, descriptor, report); see :func:`fetch_worldcover_windows`."""
    windows = fetch_worldcover_windows(client, store, repository, aoi_id=aoi_id, wgs84_bounds=wgs84_bounds,
                                       version=version, max_bytes=max_bytes, now=now)
    if len(windows) != 1:
        raise ValueError("AOI sur plusieurs tuiles WorldCover : utiliser fetch_worldcover_windows.")
    return windows[0][1:]


def _fetch_tile_window(client, store, repository, adapter, tile, *, aoi_id: str, wgs84_bounds, max_bytes: int,
                       now: datetime | None):
    """Return (path, descriptor, report) for the WorldCover window of one tile over (part of) an AOI.

    Cache by AOI and version: an existing stored window covering the bounds is
    reused without network (its recorded descriptor is returned). Otherwise
    the window is read by HTTP Range within ``max_bytes`` (header + tiles) and
    published under a new key. ``client`` is an ``ExternalHttpClient``; it is
    only used on a cache miss.
    """
    from src.data.layer_store import sha256_file
    from src.data.remote_window import ByteBudget, HttpRangeSource, RangeFile, read_remote_window

    if not aoi_id.replace("_", "").replace("-", "").isalnum():
        raise ValueError("Identifiant d'AOI invalide.")
    version = adapter.version
    url = adapter.tile_url(tile)
    key = f"worldcover/{Path(url).stem}_{aoi_id}.tif"
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    report = {"storage_key": key, "network_bytes": 0, "requests": 0, "reused": True}
    for descriptor, stored_key in repository.find_with_storage(adapter.source_id, wgs84_bounds):
        if stored_key == key and descriptor.version == version and store.exists(key):
            if sha256_file(store.path(key)) != descriptor.quality.checksum_sha256:
                raise ValueError("Fenêtre WorldCover locale modifiée depuis son enregistrement.")
            if _covers(store.path(key), wgs84_bounds):
                return store.path(key), descriptor, report
    if store.exists(key):
        path = store.path(key)
        if not _covers(path, wgs84_bounds):
            raise ValueError("Fenêtre WorldCover existante ne couvrant pas l'AOI : choisir un autre identifiant.")
        modified = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
        descriptor = adapter.describe_local(path, retrieved_at=modified, source_url=url, extra_provenance={
            "retrieved_at_basis": "date du fichier local (récupération antérieure au catalogue)",
            "aoi_id": aoi_id, "aoi_bounds_wgs84": ",".join(f"{v:.6f}" for v in wgs84_bounds)})
        repository.save(descriptor, key)
        return path, descriptor, report
    budget = ByteBudget(max_bytes)
    remote = RangeFile(HttpRangeSource(client, url), budget, label=url)
    data, profile, estimate = read_remote_window(remote, wgs84_bounds, "EPSG:4326")
    temporary = store.temporary(key)
    try:
        with rasterio.open(temporary, "w", **profile) as destination:
            destination.write(data, 1)
        path = store.publish(temporary, key)
    finally:
        temporary.unlink(missing_ok=True)
    descriptor = adapter.describe_local(path, retrieved_at=now, source_url=url, extra_provenance={
        "retrieved_at_basis": "requête HTTP Range", "aoi_id": aoi_id,
        "aoi_bounds_wgs84": ",".join(f"{v:.6f}" for v in wgs84_bounds),
        "window": f"col={estimate.window.col_off},row={estimate.window.row_off},"
                  f"w={estimate.width},h={estimate.height}",
        "range_bytes": remote.received, "remote_size_bytes": remote.size})
    repository.save(descriptor, key)
    report.update(network_bytes=remote.received, requests=len(remote.blocks) + 1, reused=False,
                  estimated_tile_bytes=estimate.compressed_bytes, remote_size_bytes=remote.size)
    return path, descriptor, report
