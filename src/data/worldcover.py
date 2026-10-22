"""ESA WorldCover adapter: tile planning and local description, no hidden I/O.

Tiles are 3° × 3° Cloud Optimized GeoTIFFs in EPSG:4326 (1/12 000°, ≈10 m),
named after their south-west corner. Planning never downloads; a COG window
read over the AOI (HTTP Range) is the intended retrieval, not full tiles.
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

    def describe_local(self, path, *, retrieved_at: datetime, source_url: str | None = None
                       ) -> ExternalLayerDescriptor:
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
