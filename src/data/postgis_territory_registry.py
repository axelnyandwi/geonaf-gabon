"""Read-only PostGIS copy of the territory registry (D2-a); the file registry stays the reference.

Schema: ``db/sql/0001_territorial_registry.up.sql``. Three parts, no UI:

* **Rows** (pure): a :class:`TerritoryRecord` becomes one ``territory_versions`` row whose
  ``record_document`` is the record exactly as fingerprinted by the file registry. Reading back
  rebuilds the record from that document, recomputes its SHA-256 fingerprint and checks it, plus
  the key columns and the release, against the row: a row edited behind GEONAF's back is refused.
* **Geometry** (pure): the database stores the WGS84 union as a MultiPolygon. The fingerprint
  (``src.geo.aoi_identity``) hashes the WKB of the union's *own* type, so a Polygon is wrapped in a
  one-part MultiPolygon and the original type is stored next to it; reading unwraps it and the
  fingerprint is recomputed. Coordinates travel as WKB (binary doubles, no rounding).
* **Import** (:func:`plan_import`, then :func:`apply_import`): the plan only reads the file
  registry and verifies every file, fingerprint and version chain; nothing is written there. The
  database step runs in one transaction under an advisory lock, inserts what is missing, accepts
  what is already identical and refuses any divergence (same territory and version, other
  content). Without ``apply`` the transaction is rolled back. Licences are never created
  implicitly: they come from the migration.

Areas are copied from the record (EPSG:6933, computed by GEONAF), never recomputed by PostGIS.
``psycopg`` is imported only when a database is used: the file backend never needs it.

R5 bis: every transaction that names a PostGIS function first sets, local to the transaction, the
search path ``pg_catalog, <PostGIS schema>, pg_temp`` (:mod:`src.data.postgis_session`), so that an
object of ``public`` can never shadow PostGIS. The application reads through guarded sessions
(environment mark, read only, timeout); tools and tests may pass a plain ``connect`` factory.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
from typing import Callable, Mapping

from src.data.postgis_session import plain_read_transaction, set_postgis_search_path
from src.data.territory_registry import (
    _ID, TerritoryRecord, TerritoryRegistry, TerritorySource, record_from_dict, record_to_dict,
)
from src.geo.aoi_identity import GEOMETRY_IDENTITY_VERSION, geometry_fingerprint, validate_polygonal_frame

URL_VARIABLE = "GEONAF_DATABASE_URL"
IMPORT_LOCK = "geonaf.territory_import"
# Columns checked against the record rebuilt from record_document when reading.
CHECKED_COLUMNS = ("territory_id", "version", "kind", "label", "legal_status", "geometry_sha256", "file_sha256",
                   "storage_key", "supersedes", "record_fingerprint")


class TerritoryImportError(ValueError):
    """The registry cannot be imported as is (missing licence, divergence, inconsistent file)."""


class ReadOnlyRegistryError(RuntimeError):
    """The PostGIS registry is a read-only copy in D2: registration goes through the file registry."""


# --- Geometry ------------------------------------------------------------------------------------


def wgs84_union(path):
    """WGS84 union of a geometry file, computed as ``load_aoi_identity`` does before fingerprinting."""
    import geopandas as gpd

    frame = gpd.read_file(path)
    validate_polygonal_frame(frame)
    return frame.to_crs("EPSG:4326").geometry.union_all()


def to_stored_geometry(geometry):
    """(MultiPolygon, original type) as stored; refuses anything but a 2D Polygon or MultiPolygon."""
    from shapely.geometry import MultiPolygon

    if geometry.has_z:
        raise ValueError("Géométrie 3D non prise en charge par le registre.")
    if geometry.geom_type == "Polygon":
        return MultiPolygon([geometry]), "Polygon"
    if geometry.geom_type == "MultiPolygon":
        return geometry, "MultiPolygon"
    raise ValueError(f"Géométrie non surfacique : {geometry.geom_type}.")


def from_stored_geometry(multipolygon, source_type: str):
    """Inverse of :func:`to_stored_geometry`: the geometry whose WKB the fingerprint hashes."""
    if multipolygon.geom_type != "MultiPolygon":
        raise ValueError("MultiPolygon attendu dans la base.")
    if source_type == "Polygon":
        if len(multipolygon.geoms) != 1:
            raise ValueError("Polygon d'origine stocké avec plusieurs parties.")
        return multipolygon.geoms[0]
    if source_type == "MultiPolygon":
        return multipolygon
    raise ValueError(f"Type géométrique d'origine inconnu : {source_type}.")


@dataclass(frozen=True)
class GeometryRow:
    geometry_sha256: str
    source_geometry_type: str
    wkb: bytes                 # MultiPolygon, little-endian 2D WKB (lossless doubles).
    parts: int
    vertices: int


def geometry_row(path, expected_sha256: str) -> GeometryRow:
    """Row of ``geonaf.geometries`` for a registered file, after checking the stored form keeps the fingerprint."""
    import shapely

    union = wgs84_union(path)
    if geometry_fingerprint(union) != expected_sha256:
        raise TerritoryImportError("Empreinte géométrique différente de l'enregistrement.")
    stored, source_type = to_stored_geometry(union)
    wkb = shapely.to_wkb(stored, byte_order=1, include_srid=False)
    if geometry_fingerprint(from_stored_geometry(shapely.from_wkb(wkb), source_type)) != expected_sha256:
        raise TerritoryImportError("La conversion en MultiPolygon ne conserve pas l'empreinte géométrique.")
    return GeometryRow(expected_sha256, source_type, wkb, len(stored.geoms), int(shapely.get_num_coordinates(union)))


# --- Rows ----------------------------------------------------------------------------------------


def release_key(source: TerritorySource) -> tuple:
    """Identity of a source release: every source field except the retrieval date (kept per version)."""
    return (source.source_id, source.product, source.version, source.license_spdx, source.attribution, source.url,
            source.share_alike, source.reference_year)


def version_row(record: TerritoryRecord) -> dict:
    """Columns of ``geonaf.territory_versions`` for one record, except release_id and import_id."""
    admin, grid = record.admin, record.grid
    return {
        "territory_id": record.territory_id, "version": record.version, "kind": record.kind,
        "label": record.label, "legal_status": record.legal_status, "legal_evidence": record.legal_evidence,
        "retrieved_at_utc": record.source.retrieved_at_utc,
        "geometry_sha256": record.geometry_sha256, "file_sha256": record.file_sha256,
        "storage_key": record.storage_key, "wgs84_bounds": list(record.wgs84_bounds),
        "area_km2": record.area_km2, "vertices": record.vertices, "parts": record.parts,
        "created_at_utc": record.created_at_utc,
        "country_iso3": admin.country_iso3 if admin else None,
        "admin_level_code": admin.level_code if admin else None,
        "local_type": admin.local_type if admin else None,
        "source_unit_id": admin.source_unit_id if admin else None,
        "parent_territory_id": admin.parent_territory_id if admin else None,
        "grid_system": grid.grid_system if grid else None,
        "grid_cell_id": grid.cell_id if grid else None,
        "grid_cell_size_deg": grid.cell_size_deg if grid else None,
        "scientific_profile_id": record.scientific_profile_id, "limits": list(record.limits),
        "supersedes": record.supersedes, "schema": record.schema,
        "record_fingerprint": record.fingerprint, "record_document": record_to_dict(record),
    }


def record_from_row(row: Mapping) -> TerritoryRecord:
    """Rebuild a record from a database row; refuses any row that no longer matches its fingerprint."""
    record = record_from_dict(dict(row["record_document"]))
    expected = version_row(record)
    if record.fingerprint != row["record_fingerprint"]:
        raise ValueError("Enregistrement de territoire modifié dans la base depuis son import.")
    if any(row[column] != expected[column] for column in CHECKED_COLUMNS):
        raise ValueError("Colonnes de territoire incohérentes avec le document d'origine.")
    stored_release = (row["source_id"], row["product"], row["source_version"], row["licence_id"], row["attribution"],
                      row["url"], row["share_alike"], row["reference_year"])
    if stored_release != release_key(record.source) or row["retrieved_at_utc"] != record.source.retrieved_at_utc:
        raise ValueError("Source de territoire incohérente avec le document d'origine.")
    return record


# --- Read-only repository ------------------------------------------------------------------------

_SELECT = """
SELECT v.territory_id, v.version, v.kind, v.label, v.legal_status, v.geometry_sha256, v.file_sha256,
       v.storage_key, v.supersedes, v.record_fingerprint, v.record_document, v.retrieved_at_utc,
       r.source_id, r.product, r.version AS source_version, r.licence_id, r.attribution, r.url,
       r.share_alike, r.reference_year
FROM {table} AS v JOIN geonaf.source_releases AS r ON r.release_id = v.release_id
"""
QUERIES = {
    "versions": _SELECT.format(table="geonaf.territory_versions")
    + "WHERE v.territory_id = %(territory_id)s ORDER BY v.version",
    "latest": _SELECT.format(table="geonaf.territories_latest")
    + "WHERE (%(kind)s::text IS NULL OR v.kind = %(kind)s::text) ORDER BY v.territory_id",
    "latest_by_geometry": _SELECT.format(table="geonaf.territories_latest")
    + "WHERE v.geometry_sha256 = %(geometry_sha256)s ORDER BY v.territory_id",
    "latest_intersecting": _SELECT.format(table="geonaf.territories_latest")
    + "JOIN geonaf.geometries AS g ON g.geometry_sha256 = v.geometry_sha256 "
      "WHERE ST_Intersects(g.geom, ST_MakeEnvelope(%(west)s, %(south)s, %(east)s, %(north)s, 4326)) "
      "AND (%(kind)s::text IS NULL OR v.kind = %(kind)s::text) ORDER BY v.territory_id",
    "geometry": "SELECT source_geometry_type, ST_AsBinary(geom, 'NDR') AS wkb FROM geonaf.geometries "
                "WHERE geometry_sha256 = %(geometry_sha256)s",
}


def psycopg_conninfo(url: str) -> str:
    """libpq URI from the SQLAlchemy URL used by Alembic (``postgresql+psycopg://`` → ``postgresql://``)."""
    if url.startswith("postgresql+psycopg://"):
        return "postgresql://" + url[len("postgresql+psycopg://"):]
    if url.startswith("postgresql://"):
        return url
    raise ValueError("URL PostgreSQL attendue (postgresql+psycopg://…).")


def connect_from_env(variable: str = URL_VARIABLE) -> Callable:
    """Connection factory reading the URL at each call; the URL is never logged or shown."""
    def connect():
        import psycopg

        url = os.environ.get(variable, "").strip()
        if not url:
            raise RuntimeError(f"{variable} n'est pas défini.")
        return psycopg.connect(psycopg_conninfo(url))
    return connect


class PostgisTerritoryRegistry:
    """Same read methods as :class:`TerritoryRegistry`; geometries come from the database (``geometry``).

    ``sessions``: guarded application sessions (:class:`src.data.postgis_session.PostgisSessions`).
    ``connect``: a bare connection factory (tools, tests); its transactions get the R5 bis path too.
    ``data_dir`` is optional: only :meth:`geometry_path` reads files (and verifies their digest).
    """

    def __init__(self, connect: Callable | None = None, data_dir=None, *, sessions=None):
        self._connect = connect
        self._sessions = sessions
        self.files = TerritoryRegistry(Path(data_dir)) if data_dir is not None else None

    def _transaction(self):
        if self._sessions is not None:
            return self._sessions.read()
        return plain_read_transaction(self._connect)

    def _rows(self, query: str, params: dict) -> list[dict]:
        from psycopg.rows import dict_row

        with self._transaction() as connection:
            with connection.cursor(row_factory=dict_row) as cursor:
                cursor.execute(QUERIES[query], params)
                return cursor.fetchall()

    def versions(self, territory_id: str) -> tuple[TerritoryRecord, ...]:
        if not _ID.fullmatch(territory_id or ""):
            raise ValueError("Identifiant de territoire invalide.")
        return tuple(record_from_row(row) for row in self._rows("versions", {"territory_id": territory_id}))

    def latest(self, territory_id: str) -> TerritoryRecord | None:
        versions = self.versions(territory_id)
        return versions[-1] if versions else None

    def get(self, territory_id: str, version: int | None = None) -> TerritoryRecord:
        versions = self.versions(territory_id)
        match = versions[-1:] if version is None else [r for r in versions if r.version == version]
        if not match:
            raise KeyError(f"Territoire inconnu : {territory_id}" + ("" if version is None else f" v{version}"))
        return match[0]

    def territories(self, kind: str | None = None) -> tuple[TerritoryRecord, ...]:
        """Latest version of every territory, optionally of one kind."""
        return tuple(record_from_row(row) for row in self._rows("latest", {"kind": kind}))

    def find_geometry(self, geometry_sha256: str) -> tuple[TerritoryRecord, ...]:
        """Every territory (latest versions) registered with this exact geometry."""
        return tuple(record_from_row(row) for row in
                     self._rows("latest_by_geometry", {"geometry_sha256": geometry_sha256}))

    def intersecting(self, wgs84_bounds, kind: str | None = None) -> tuple[TerritoryRecord, ...]:
        """Latest versions whose geometry intersects a WGS84 rectangle (GiST index)."""
        west, south, east, north = (float(v) for v in wgs84_bounds)
        params = {"west": west, "south": south, "east": east, "north": north, "kind": kind}
        return tuple(record_from_row(row) for row in self._rows("latest_intersecting", params))

    def geometry(self, record: TerritoryRecord):
        """Stored geometry in its original type, checked against the record's fingerprint."""
        import shapely

        rows = self._rows("geometry", {"geometry_sha256": record.geometry_sha256})
        if not rows:
            raise KeyError("Géométrie absente de la base.")
        geometry = from_stored_geometry(shapely.from_wkb(bytes(rows[0]["wkb"])), rows[0]["source_geometry_type"])
        if geometry_fingerprint(geometry) != record.geometry_sha256:
            raise ValueError("Géométrie de la base différente de son empreinte.")
        return geometry

    def geometry_path(self, record: TerritoryRecord) -> Path:
        if self.files is None:
            raise FileNotFoundError("Registre PostGIS sans répertoire de fichiers : utiliser geometry().")
        return self.files.geometry_path(record)

    def register(self, **_):
        raise ReadOnlyRegistryError("Registre PostGIS en lecture seule : enregistrer dans le registre fichier.")


# --- Import --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ImportPlan:
    records: tuple[TerritoryRecord, ...]            # Ordered by territory, then version.
    geometries: Mapping[str, GeometryRow]

    @property
    def fingerprints_sha256(self) -> str:
        joined = "\n".join(sorted(r.fingerprint for r in self.records))
        return hashlib.sha256(joined.encode("ascii")).hexdigest()

    @property
    def releases(self) -> tuple[tuple, ...]:
        return tuple(sorted({release_key(r.source) for r in self.records}, key=repr))

    @property
    def licence_ids(self) -> tuple[str, ...]:
        return tuple(sorted({r.source.license_spdx for r in self.records}))


@dataclass(frozen=True)
class ImportReport:
    applied: bool
    seen: int
    inserted: int                # Without apply: rows that would have been inserted.
    existing: int
    geometries: int
    releases: int
    fingerprints_sha256: str
    import_id: int | None


def plan_import(registry: TerritoryRegistry) -> ImportPlan:
    """Read and verify the whole file registry; writes nothing anywhere."""
    records, geometries = [], {}
    for latest in registry.territories():
        versions = registry.versions(latest.territory_id)          # Each record's fingerprint is checked here.
        for number, record in enumerate(versions, start=1):
            previous = versions[number - 2].fingerprint if number > 1 else None
            if record.version != number or record.supersedes != previous:
                raise TerritoryImportError(f"Chaîne de versions incohérente : {record.territory_id}.")
            path = registry.geometry_path(record)                   # File digest checked here.
            if record.geometry_sha256 not in geometries:
                geometries[record.geometry_sha256] = geometry_row(path, record.geometry_sha256)
            stored = geometries[record.geometry_sha256]
            if (stored.parts, stored.vertices) != (record.parts, record.vertices):
                raise TerritoryImportError(f"Parties ou sommets incohérents : {record.territory_id}.")
            records.append(record)
    return ImportPlan(tuple(records), geometries)


def _release_id(cursor, key: tuple) -> int:
    source_id, product, version, licence_id, attribution, url, share_alike, reference_year = key
    cursor.execute(
        "SELECT release_id FROM geonaf.source_releases WHERE source_id = %s AND product = %s AND version = %s "
        "AND licence_id = %s AND attribution = %s AND url IS NOT DISTINCT FROM %s::text AND share_alike = %s "
        "AND reference_year IS NOT DISTINCT FROM %s::integer", key)
    row = cursor.fetchone()
    if row:
        return row[0]
    cursor.execute(
        "INSERT INTO geonaf.source_releases (source_id, product, version, licence_id, attribution, url, share_alike, "
        "reference_year) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING release_id", key)
    return cursor.fetchone()[0]


def _store_geometry(cursor, row: GeometryRow) -> None:
    import shapely

    cursor.execute(
        "INSERT INTO geonaf.geometries (geometry_sha256, identity_version, source_geometry_type, geom, parts, vertices) "
        "VALUES (%s, %s, %s, ST_GeomFromWKB(%s, 4326), %s, %s) ON CONFLICT (geometry_sha256) DO NOTHING",
        (row.geometry_sha256, GEOMETRY_IDENTITY_VERSION, row.source_geometry_type, row.wkb, row.parts, row.vertices))
    cursor.execute("SELECT source_geometry_type, ST_AsBinary(geom, 'NDR'), parts, vertices FROM geonaf.geometries "
                   "WHERE geometry_sha256 = %s", (row.geometry_sha256,))
    source_type, wkb, parts, vertices = cursor.fetchone()
    same = shapely.from_wkb(bytes(wkb)).equals_exact(shapely.from_wkb(row.wkb), 0)
    if (source_type, parts, vertices) != (row.source_geometry_type, row.parts, row.vertices) or not same:
        raise TerritoryImportError("Géométrie déjà présente dans la base avec un autre contenu.")


def apply_import(connection, plan: ImportPlan, *, source_label: str, code_version: str,
                 apply: bool = False) -> ImportReport:
    """Insert what is missing in one transaction; without ``apply`` everything is rolled back."""
    import psycopg
    from psycopg.types.json import Jsonb

    inserted = existing = 0
    import_id = None
    with connection.transaction(), connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (IMPORT_LOCK,))
        set_postgis_search_path(cursor)                    # R5 bis: ST_GeomFromWKB and ST_AsBinary below.
        cursor.execute("SELECT licence_id, share_alike FROM geonaf.licences WHERE licence_id = ANY(%s)",
                       (list(plan.licence_ids),))
        licences = dict(cursor.fetchall())
        missing = sorted(set(plan.licence_ids) - set(licences))
        if missing:
            raise TerritoryImportError("Licences absentes de la base (à ajouter par migration) : "
                                       + ", ".join(missing))
        for key in plan.releases:
            if key[6] != licences[key[3]]:
                raise TerritoryImportError(f"Partage à l'identique incohérent avec la licence {key[3]}.")
        cursor.execute("INSERT INTO geonaf.registry_imports (source_label, code_version) VALUES (%s, %s) "
                       "RETURNING import_id", (source_label, code_version))
        import_id = cursor.fetchone()[0]
        for source_id in sorted({key[0] for key in plan.releases}):
            cursor.execute("INSERT INTO geonaf.data_sources (source_id) VALUES (%s) ON CONFLICT DO NOTHING",
                           (source_id,))
        releases = {key: _release_id(cursor, key) for key in plan.releases}
        for sha in sorted(plan.geometries):
            _store_geometry(cursor, plan.geometries[sha])
        for record in plan.records:
            cursor.execute("INSERT INTO geonaf.territories (territory_id, kind) VALUES (%s, %s) "
                           "ON CONFLICT (territory_id) DO NOTHING", (record.territory_id, record.kind))
            cursor.execute("SELECT kind FROM geonaf.territories WHERE territory_id = %s", (record.territory_id,))
            if cursor.fetchone()[0] != record.kind:
                raise TerritoryImportError(f"Type de territoire différent dans la base : {record.territory_id}.")
            cursor.execute("SELECT record_fingerprint FROM geonaf.territory_versions "
                           "WHERE territory_id = %s AND version = %s", (record.territory_id, record.version))
            found = cursor.fetchone()
            if found:
                if found[0] != record.fingerprint:
                    raise TerritoryImportError(f"Divergence avec la base : {record.territory_id} "
                                               f"v{record.version} a un autre contenu.")
                existing += 1
                continue
            row = version_row(record)
            row.update(release_id=releases[release_key(record.source)], import_id=import_id,
                       record_document=Jsonb(row["record_document"]))
            columns = list(row)
            values = ", ".join(f"%({c})s::float8[]" if c == "wgs84_bounds" else
                               f"%({c})s::text[]" if c == "limits" else f"%({c})s" for c in columns)
            cursor.execute(f"INSERT INTO geonaf.territory_versions ({', '.join(columns)}) VALUES ({values})", row)
            inserted += 1
        cursor.execute("UPDATE geonaf.registry_imports SET finished_at = now(), records_seen = %s, "
                       "records_inserted = %s, records_existing = %s, fingerprints_sha256 = %s "
                       "WHERE import_id = %s",
                       (len(plan.records), inserted, existing, plan.fingerprints_sha256, import_id))
        if not apply:
            raise psycopg.Rollback()
    return ImportReport(apply, len(plan.records), inserted, existing, len(plan.geometries), len(plan.releases),
                        plan.fingerprints_sha256, import_id if apply else None)
