"""OpenStreetMap adapter: bounded Overpass queries, cached snapshots, strict geometry.

* :meth:`OpenStreetMapAdapter.overpass_query` builds a bounded query (bbox ≤
  0.5°, validated tag filters, server timeout and memory ceiling). Ways and
  ``type=multipolygon`` relations are requested with ``out geom``.
* :meth:`OpenStreetMapAdapter.parse_overpass` converts the response offline.
  Multipolygon relations are assembled from their member ways (outer/inner
  rings); a geometry is kept only when every ring closes and the result is
  valid. Incomplete or invalid objects are counted by reason, never repaired
  silently. A response carrying an Overpass runtime ``remark`` (timeout,
  memory) is partial and is rejected.
* :func:`fetch_osm_snapshot` sends the query through the allow-listed client
  only on a cache miss, stores the raw response once (never overwritten), and
  records the descriptor. The snapshot date is the OSM database timestamp,
  not the retrieval date.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
import hashlib
import json

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
MAX_SERVER_MEMORY = 64 * 1024 * 1024
# Measured on Akanda (22/10/2026): 24 267 051 bytes for 39 164 elements ≈ 620 B; margin included.
ESTIMATED_BYTES_PER_ELEMENT = 800
GROUPS = {  # Display groups; detail stays in ``category``.
    "building": "Bâtiments", "highway": "Routes et voies", "waterway": "Eau (OSM)", "natural:water": "Eau (OSM)",
    "natural:wetland": "Zones humides (OSM)", "natural:wood": "Végétation (OSM)", "natural:scrub": "Végétation (OSM)",
    "landuse:forest": "Végétation (OSM)", "natural:beach": "Plages (OSM)",
}


def osm_group(category: str) -> str:
    if category in GROUPS:
        return GROUPS[category]
    return "Usage du sol (OSM)" if category.startswith("landuse:") else "Autres (OSM)"


class IncompleteOverpassResponse(ValueError):
    """The server reported a runtime error: the extract is partial and unusable."""


def _validated_bounds(bounds):
    left, bottom, right, top = (float(value) for value in bounds)
    if not (-180 <= left < right <= 180 and -90 <= bottom < top <= 90):
        raise ValueError("Emprise WGS84 invalide.")
    if right - left > MAX_BBOX_DEGREES or top - bottom > MAX_BBOX_DEGREES:
        raise ValueError("Emprise trop grande pour une requête Overpass bornée.")
    return left, bottom, right, top


def _coordinates(points):
    """Coordinates of an ``out geom`` member, or None when a vertex is missing."""
    if not isinstance(points, list) or len(points) < 2:
        return None
    output = []
    for point in points:
        if not isinstance(point, dict) or point.get("lat") is None or point.get("lon") is None:
            return None
        output.append((float(point["lon"]), float(point["lat"])))
    return output


def _assemble_rings(lines):
    """Polygons formed by member lines, or None when a ring does not close."""
    from shapely import polygonize_full
    from shapely.geometry import LineString
    from shapely.ops import unary_union

    if not lines:
        return None
    noded = unary_union([LineString(line) for line in lines])
    polygons, cuts, dangles, invalid = polygonize_full([noded])
    if not cuts.is_empty or not dangles.is_empty or not invalid.is_empty or polygons.is_empty:
        return None
    return unary_union(list(polygons.geoms))


class OpenStreetMapAdapter:
    source_id = "openstreetmap"

    def overpass_query(self, wgs84_bounds, tags=None, *, timeout_s: int = 60) -> str:
        left, bottom, right, top = _validated_bounds(wgs84_bounds)
        if not 1 <= int(timeout_s) <= 90:
            raise ValueError("Délai Overpass hors bornes.")
        bbox = f"{bottom:.6f},{left:.6f},{top:.6f},{right:.6f}"  # Overpass order: S,W,N,E.
        clauses = []
        for key, values in (tags or DEFAULT_TAGS).items():
            if not key.replace("_", "").isalnum():
                raise ValueError("Clé OSM invalide.")
            if values is None:
                selector = f'["{key}"]'
            else:
                if not all(value.replace("_", "").isalnum() for value in values):
                    raise ValueError("Valeur OSM invalide.")
                selector = f'["{key}"~"^({"|".join(values)})$"]'
            clauses.append(f"  way{selector}({bbox});")
            if key in AREA_KEYS:
                clauses.append(f'  relation{selector}["type"="multipolygon"]({bbox});')
        return (f"[out:json][timeout:{int(timeout_s)}][maxsize:{MAX_SERVER_MEMORY}];\n(\n"
                + "\n".join(clauses) + "\n);\nout geom;")

    def count_query(self, wgs84_bounds, tags=None, *, timeout_s: int = 60) -> str:
        """Same selection, ``out count``: a few hundred bytes used to estimate the full response."""
        return self.overpass_query(wgs84_bounds, tags, timeout_s=timeout_s).replace("out geom;", "out count;")

    def plan(self, wgs84_bounds) -> RetrievalPlan:
        return RetrievalPlan(self.source_id, (self.overpass_query(wgs84_bounds),), None, (
            f"Point d'accès : {OVERPASS_ENDPOINT} (politique d'usage Overpass : requêtes rares et mises en cache).",
            "Envoi uniquement par fetch_osm_snapshot, sur action explicite, en l'absence de cache récent."))

    @staticmethod
    def _category(tags):
        for key in DEFAULT_TAGS:
            if key in tags:
                return key if key in ("building", "highway", "waterway") else f"{key}:{tags[key]}"
        return "other"

    def _way_geometry(self, element, tags):
        from shapely.geometry import LineString, Polygon

        coordinates = _coordinates(element.get("geometry"))
        if coordinates is None:
            return None, "incomplete"
        closed = coordinates[0] == coordinates[-1] and len(coordinates) >= 4
        is_area = (AREA_KEYS & set(tags)) or ("highway" in tags and tags.get("area") == "yes")
        shape = Polygon(coordinates) if closed and is_area else LineString(coordinates)
        if is_area and not closed and not ({"highway", "waterway"} & set(tags)):
            return None, "incomplete"  # An area tag on an open way cannot form a surface.
        return (shape, None) if shape.is_valid and not shape.is_empty else (None, "invalid")

    def _relation_geometry(self, element):
        outer, inner = [], []
        for member in element.get("members") or []:
            if member.get("type") != "way":
                continue  # Nodes (labels) and sub-relations are not part of the rings.
            coordinates = _coordinates(member.get("geometry"))
            if coordinates is None:
                return None, "incomplete"
            (inner if member.get("role") == "inner" else outer).append(coordinates)
        outer_shape = _assemble_rings(outer)
        if outer_shape is None:
            return None, "incomplete"
        if inner:
            inner_shape = _assemble_rings(inner)
            if inner_shape is None:
                return None, "incomplete"
            outer_shape = outer_shape.difference(inner_shape)
        if outer_shape.is_empty or not outer_shape.is_valid or outer_shape.area <= 0:
            return None, "invalid"
        return outer_shape, None

    def parse_overpass(self, payload: dict, *, retrieved_at: datetime, wgs84_bounds, extra_provenance=None,
                       checksum_sha256: str | None = None):
        """Return (GeoDataFrame in EPSG:4326, descriptor); rejected elements are counted by reason."""
        import geopandas as gpd

        bounds = _validated_bounds(wgs84_bounds)
        remark = payload.get("remark")
        if isinstance(remark, str) and ("runtime error" in remark.lower() or "timed out" in remark.lower()):
            raise IncompleteOverpassResponse("Réponse Overpass partielle (erreur d'exécution serveur) : rejetée.")
        base = (payload.get("osm3s") or {}).get("timestamp_osm_base")
        if not isinstance(base, str):
            raise ValueError("Horodatage de la base OSM absent : provenance insuffisante.")
        snapshot = datetime.fromisoformat(base.replace("Z", "+00:00"))
        rows, rejected, kinds = [], Counter(), Counter()
        for element in payload.get("elements", []):
            tags = element.get("tags") or {}
            kind = element.get("type")
            if kind == "way":
                shape, reason = self._way_geometry(element, tags)
            elif kind == "relation" and tags.get("type") == "multipolygon":
                shape, reason = self._relation_geometry(element)
            else:
                shape, reason = None, "unsupported"
            if shape is None:
                rejected[reason] += 1
                continue
            kinds[kind] += 1
            category = self._category(tags)
            detail = tags.get(category) if category in ("highway", "building", "waterway") else None
            rows.append({"osm_id": f"{kind}/{int(element['id'])}", "category": category,
                         "group": osm_group(category), "detail": detail, "geometry": shape})
        frame = gpd.GeoDataFrame(rows, columns=["osm_id", "category", "group", "detail", "geometry"],
                                 geometry="geometry", crs="EPSG:4326")
        provenance = {"endpoint": OVERPASS_ENDPOINT,
                      "retrieved_at_utc": retrieved_at.astimezone(timezone.utc).isoformat(),
                      "osm_base_utc": snapshot.astimezone(timezone.utc).isoformat(),
                      "features": str(len(frame)), "ways": str(kinds["way"]), "relations": str(kinds["relation"]),
                      "skipped_elements": str(sum(rejected.values())),
                      "rejected_incomplete": str(rejected["incomplete"]),
                      "rejected_invalid": str(rejected["invalid"]),
                      "rejected_unsupported": str(rejected["unsupported"])}
        provenance.update({key: str(value) for key, value in (extra_provenance or {}).items()})
        descriptor = ExternalLayerDescriptor(
            source_id=self.source_id, product="OpenStreetMap (extrait Overpass)",
            version=snapshot.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ"), kind="vector",
            temporal=TemporalValidity("snapshot", snapshot, snapshot), resolution_m=None, crs="EPSG:4326",
            extent=SpatialExtent("EPSG:4326", bounds, bounds), license=LICENSE,
            provenance=provenance,
            quality=LayerQuality("unknown", (
                "Complétude variable selon les contributions ; absence d'objet ≠ absence sur le terrain.",
                "Géométries incomplètes ou invalides écartées (non réparées) ; voir les compteurs de provenance.",
                "Date de l'objet sur le terrain inconnue : seule la date de la base est connue."),
                checksum_sha256))
        return frame, descriptor


def _snapshot_name(instant: datetime) -> str:
    return instant.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + ".json"


def estimate_osm_bytes(client, wgs84_bounds, tags=None, *, timeout_s: int = 60) -> dict:
    """Element counts from ``out count`` and a conservative byte estimate (no geometry downloaded)."""
    query = OpenStreetMapAdapter().count_query(wgs84_bounds, tags, timeout_s=timeout_s)
    response = client.request("POST", OVERPASS_ENDPOINT, data={"data": query}, max_bytes=64 * 1024,
                              timeout=(10.0, float(timeout_s) + 30.0))
    payload = json.loads(response.content.decode("utf-8"))
    counts = next((element.get("tags", {}) for element in payload.get("elements", [])
                   if element.get("type") == "count"), None)
    if not counts or not str(counts.get("total", "")).isdigit():
        raise ValueError("Comptage Overpass illisible : estimation impossible.")
    total = int(counts["total"])
    return {"elements": total, "ways": int(counts.get("ways", 0)), "relations": int(counts.get("relations", 0)),
            "estimated_bytes": total * ESTIMATED_BYTES_PER_ELEMENT, "count_bytes": len(response.content)}


def fetch_osm_snapshot(client, store, repository, *, wgs84_bounds, tags=None, timeout_s: int = 60,
                       max_age: timedelta = timedelta(days=7), force_refresh: bool = False,
                       now: datetime | None = None, max_bytes: int = 32 * 1024 * 1024, estimate: bool = False):
    """Return (frame, descriptor, report); network only on a cache miss or explicit refresh.

    Cache: per query digest (AOI bounds + tags + options). The most recent
    stored snapshot younger than ``max_age`` is reused. A refresh writes a new
    snapshot file; older ones are kept for reproducibility.
    """
    adapter = OpenStreetMapAdapter()
    query = adapter.overpass_query(wgs84_bounds, tags, timeout_s=timeout_s)
    digest = hashlib.sha256(query.encode("utf-8")).hexdigest()
    prefix = f"openstreetmap/{digest[:24]}"
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    report = {"query_sha256": digest, "network_bytes": 0, "requests": 0, "reused": True}
    stored = [key for key in store.keys(prefix) if key.endswith(".json")]
    if stored and not force_refresh:
        key = stored[-1]  # Names sort chronologically.
        retrieved = datetime.strptime(key.rsplit("/", 1)[1][:-5], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        if now - retrieved <= max_age:
            frame, descriptor = load_osm_snapshot(store, key, wgs84_bounds, expected_query_sha256=digest)
            report.update(storage_key=key, age_hours=round((now - retrieved).total_seconds() / 3600, 1))
            repository.save(descriptor, key)
            return frame, descriptor, report
    if estimate:
        estimation = estimate_osm_bytes(client, wgs84_bounds, tags, timeout_s=timeout_s)
        report["estimate"] = estimation
        if estimation["estimated_bytes"] > max_bytes:
            raise ValueError(f"Volume OSM estimé ({estimation['estimated_bytes'] // 1_000_000} Mo) supérieur au "
                             f"plafond ({max_bytes // 1_000_000} Mo) : requête non envoyée.")
    response = client.request("POST", OVERPASS_ENDPOINT, data={"data": query}, max_bytes=max_bytes,
                              timeout=(10.0, float(timeout_s) + 30.0))
    payload = json.loads(response.content.decode("utf-8"))
    meta = {"query": query, "query_sha256": digest, "endpoint": OVERPASS_ENDPOINT,
            "retrieved_at_utc": now.isoformat(), "response_bytes": len(response.content),
            "response_sha256": hashlib.sha256(response.content).hexdigest()}
    # Parse before storing: a partial response must not enter the cache.
    adapter.parse_overpass(payload, retrieved_at=now, wgs84_bounds=wgs84_bounds)
    key = f"{prefix}/{_snapshot_name(now)}"
    wrapper = {"geonaf": meta, "response": payload}
    store.write_bytes(key, json.dumps(wrapper, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    frame, descriptor = load_osm_snapshot(store, key, wgs84_bounds, expected_query_sha256=digest)
    repository.save(descriptor, key)
    report.update(storage_key=key, network_bytes=len(response.content), requests=1, reused=False, age_hours=0.0)
    return frame, descriptor, report


def load_osm_snapshot(store, key: str, wgs84_bounds, *, expected_query_sha256: str | None = None):
    """Parse a stored snapshot offline; the file digest becomes the descriptor checksum.

    ``stored_layers`` compares that checksum with the recorded one, so a
    modified snapshot is detected before use.
    """
    raw = store.path(key).read_bytes()
    wrapper = json.loads(raw.decode("utf-8"))
    meta = wrapper["geonaf"]
    if expected_query_sha256 is not None and meta["query_sha256"] != expected_query_sha256:
        raise ValueError("Instantané OSM incohérent avec la requête.")
    retrieved = datetime.fromisoformat(meta["retrieved_at_utc"])
    return OpenStreetMapAdapter().parse_overpass(wrapper["response"], retrieved_at=retrieved,
                                                 wgs84_bounds=wgs84_bounds,
                                                 extra_provenance=_snapshot_provenance(wrapper, key),
                                                 checksum_sha256=hashlib.sha256(raw).hexdigest())


def _snapshot_provenance(wrapper, key):
    meta = wrapper["geonaf"]
    return {"query_sha256": meta["query_sha256"], "response_sha256": meta["response_sha256"],
            "response_bytes": meta["response_bytes"], "storage_key": key}
