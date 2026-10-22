"""OpenStreetMap adapter: bounded Overpass query text and offline parsing.

The query is built but never sent here. Parsing reads an Overpass JSON
response (``out geom``) already obtained by an authorised step. The snapshot
date is the OSM database timestamp, not the retrieval date. Relations
(multipolygons) are not assembled in this preparation step.
"""

from __future__ import annotations

from datetime import datetime, timezone

from src.data.external_sources import (
    ExternalLayerDescriptor, LayerLicense, LayerQuality, RetrievalPlan, SpatialExtent, TemporalValidity,
)

OVERPASS_ENDPOINT = "https://overpass-api.de/api/interpreter"
LICENSE = LayerLicense("ODbL-1.0", "© OpenStreetMap contributors",
                       "https://www.openstreetmap.org/copyright", share_alike=True)
# Tag filters relevant to vegetation-change context; None means any value.
DEFAULT_TAGS = {"building": None, "highway": None, "landuse": None,
                "natural": ("water", "wetland", "wood", "scrub", "beach"), "waterway": None}
AREA_KEYS = {"building", "landuse", "natural"}
MAX_BBOX_DEGREES = 0.5


def _validated_bounds(bounds):
    left, bottom, right, top = (float(value) for value in bounds)
    if not (-180 <= left < right <= 180 and -90 <= bottom < top <= 90):
        raise ValueError("Emprise WGS84 invalide.")
    if right - left > MAX_BBOX_DEGREES or top - bottom > MAX_BBOX_DEGREES:
        raise ValueError("Emprise trop grande pour une requête Overpass bornée.")
    return left, bottom, right, top


class OpenStreetMapAdapter:
    source_id = "openstreetmap"

    def overpass_query(self, wgs84_bounds, tags=None, *, timeout_s: int = 60) -> str:
        left, bottom, right, top = _validated_bounds(wgs84_bounds)
        if not 1 <= int(timeout_s) <= 180:
            raise ValueError("Délai Overpass hors bornes.")
        bbox = f"{bottom:.6f},{left:.6f},{top:.6f},{right:.6f}"  # Overpass order: S,W,N,E.
        clauses = []
        for key, values in (tags or DEFAULT_TAGS).items():
            if not key.replace("_", "").isalnum():
                raise ValueError("Clé OSM invalide.")
            if values is None:
                clauses.append(f'  way["{key}"]({bbox});')
            else:
                if not all(value.replace("_", "").isalnum() for value in values):
                    raise ValueError("Valeur OSM invalide.")
                clauses.append(f'  way["{key}"~"^({"|".join(values)})$"]({bbox});')
        return f"[out:json][timeout:{int(timeout_s)}];\n(\n" + "\n".join(clauses) + "\n);\nout geom;"

    def plan(self, wgs84_bounds) -> RetrievalPlan:
        return RetrievalPlan(self.source_id, (self.overpass_query(wgs84_bounds),), None, (
            f"Point d'accès prévu : {OVERPASS_ENDPOINT} (politique d'usage Overpass à respecter).",
            "Hôte hors de la politique réseau Copernicus actuelle : autorisation explicite requise."))

    @staticmethod
    def _category(tags):
        for key in DEFAULT_TAGS:
            if key in tags:
                return key if key in ("building", "highway", "waterway") else f"{key}:{tags[key]}"
        return "other"

    def parse_overpass(self, payload: dict, *, retrieved_at: datetime, wgs84_bounds):
        """Return (GeoDataFrame in EPSG:4326, descriptor); unsupported elements are counted."""
        import geopandas as gpd
        from shapely.geometry import LineString, Polygon

        bounds = _validated_bounds(wgs84_bounds)
        base = (payload.get("osm3s") or {}).get("timestamp_osm_base")
        if not isinstance(base, str):
            raise ValueError("Horodatage de la base OSM absent : provenance insuffisante.")
        snapshot = datetime.fromisoformat(base.replace("Z", "+00:00"))
        rows, skipped = [], 0
        for element in payload.get("elements", []):
            geometry = element.get("geometry")
            tags = element.get("tags") or {}
            if element.get("type") != "way" or not isinstance(geometry, list) or len(geometry) < 2:
                skipped += 1
                continue
            coordinates = [(float(point["lon"]), float(point["lat"])) for point in geometry]
            closed = coordinates[0] == coordinates[-1] and len(coordinates) >= 4
            shape = (Polygon(coordinates) if closed and AREA_KEYS & set(tags) else LineString(coordinates))
            if not shape.is_valid:
                skipped += 1
                continue
            rows.append({"osm_id": f"way/{int(element['id'])}", "category": self._category(tags),
                         "geometry": shape})
        frame = gpd.GeoDataFrame(rows, columns=["osm_id", "category", "geometry"], geometry="geometry",
                                 crs="EPSG:4326")
        descriptor = ExternalLayerDescriptor(
            source_id=self.source_id, product="OpenStreetMap (extrait Overpass)",
            version=snapshot.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ"), kind="vector",
            temporal=TemporalValidity("snapshot", snapshot, snapshot), resolution_m=None, crs="EPSG:4326",
            extent=SpatialExtent("EPSG:4326", bounds, bounds), license=LICENSE,
            provenance={"endpoint": OVERPASS_ENDPOINT,
                        "retrieved_at_utc": retrieved_at.astimezone(timezone.utc).isoformat(),
                        "osm_base_utc": snapshot.astimezone(timezone.utc).isoformat(),
                        "features": str(len(frame)), "skipped_elements": str(skipped)},
            quality=LayerQuality("unknown", (
                "Complétude variable selon les contributions ; absence d'objet ≠ absence sur le terrain.",
                "Relations multipolygones non assemblées dans cette version.")))
        return frame, descriptor
