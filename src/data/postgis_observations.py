"""Acquisitions and indicator results in PostGIS (migration 0002, D3-D4): import and read; no UI.

Import (:func:`apply_acquisition_import`, :func:`apply_indicator_import`), same rules as the territory
import (``src.data.postgis_territory_registry.apply_import``):

* one transaction under an advisory lock, R5 bis search path set before any PostGIS call;
* one journal row in ``geonaf.data_imports`` (scope, label, code version, counts, aggregate fingerprint);
* what is missing is inserted, what is already identical is counted as existing, any divergence (same
  identity, other content) stops everything: nothing is overwritten;
* without ``apply`` the transaction is rolled back: the report gives what *would* be inserted.

Indicator results need the geometry they were computed on in ``geonaf.geometries``: import the
territory registry first. A result is never re-attached to another geometry.

Read (:class:`PostgisAcquisitionCatalog`, :class:`PostgisIndicatorStore`): same methods as the file
classes of :mod:`src.data.observations`; every row is rebuilt from its stored document and its
fingerprint recomputed, so a row edited behind GEONAF's back is refused.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
from typing import Iterable

from src.data.observations import (
    AcquisitionRecord, AcquisitionScan, IndicatorResult, acquisition_record, result_from_envelope,
)
from src.data.postgis_session import plain_read_transaction, set_postgis_search_path

OBSERVATION_LOCK = "geonaf.observation_import"


class ObservationImportError(ValueError):
    """The observations cannot be imported as they are (missing geometry, divergence)."""


@dataclass(frozen=True)
class ObservationImportReport:
    scope: str
    applied: bool
    seen: int
    inserted: int               # Without apply: rows that would have been inserted.
    existing: int
    fingerprints_sha256: str
    import_id: int | None


def aggregate_sha256(fingerprints: Iterable[str]) -> str:
    return hashlib.sha256("\n".join(sorted(fingerprints)).encode("ascii")).hexdigest()


def _open_journal(cursor, scope: str, source_label: str, code_version: str) -> int:
    if any(mark in source_label for mark in ("/", "\\", ":")):
        raise ObservationImportError("Le libellé de source ne doit pas être un chemin.")
    cursor.execute("SELECT pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtext(%s))", (OBSERVATION_LOCK,))
    set_postgis_search_path(cursor)
    cursor.execute("INSERT INTO geonaf.data_imports (scope, source_label, code_version) VALUES (%s, %s, %s) "
                   "RETURNING import_id", (scope, source_label, code_version))
    return cursor.fetchone()[0]


def _close_journal(cursor, import_id: int, seen: int, inserted: int, existing: int, aggregate: str) -> None:
    cursor.execute("UPDATE geonaf.data_imports SET finished_at = now(), records_seen = %s, records_inserted = %s, "
                   "records_existing = %s, fingerprints_sha256 = %s WHERE import_id = %s",
                   (seen, inserted, existing, aggregate, import_id))


# --- Acquisitions ----------------------------------------------------------------------------------


def apply_acquisition_import(connection, records: Iterable[AcquisitionRecord], *, source_label: str,
                             code_version: str, apply: bool = False) -> ObservationImportReport:
    """Insert missing products and manifests in one transaction; without ``apply`` everything is rolled back."""
    import psycopg
    from psycopg.types.json import Jsonb

    records = sorted(records, key=lambda r: r.manifest_sha256)
    if len({r.manifest_sha256 for r in records}) != len(records):
        raise ObservationImportError("Manifeste présent deux fois dans le lot.")
    aggregate = aggregate_sha256(r.manifest_sha256 for r in records)
    inserted = existing = 0
    with connection.transaction(), connection.cursor() as cursor:
        import_id = _open_journal(cursor, "acquisitions", source_label, code_version)
        for record in records:
            cursor.execute("INSERT INTO geonaf.acquisitions (collection, product_id, acquired_at) VALUES (%s, %s, %s) "
                           "ON CONFLICT (collection, product_id) DO NOTHING",
                           (record.collection, record.product_id, record.acquired_at_utc))
            cursor.execute("SELECT acquired_at FROM geonaf.acquisitions WHERE collection = %s AND product_id = %s",
                           record.identity)
            if cursor.fetchone()[0] != record.acquired_at_utc:
                raise ObservationImportError(f"Date d'acquisition différente dans la base : {record.product_id}.")
            cursor.execute("SELECT manifest_document FROM geonaf.acquisition_manifests WHERE manifest_sha256 = %s",
                           (record.manifest_sha256,))
            found = cursor.fetchone()
            if found:
                if found[0] != record.document:
                    raise ObservationImportError(f"Divergence avec la base : manifeste {record.manifest_sha256[:12]}.")
                existing += 1
                continue
            cursor.execute(
                "INSERT INTO geonaf.acquisition_manifests (manifest_sha256, collection, product_id, schema_version, "
                "acquired_at, ingested_at, stac_source, grid_crs, grid_resolution_m, footprint, integrity_mode, "
                "manifest_document, import_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, "
                "ST_GeomFromWKB(%s::bytea, 4326), %s, %s, %s)",
                (record.manifest_sha256, record.collection, record.product_id, record.document["schema_version"],
                 record.acquired_at_utc, record.ingested_at_utc, record.stac_source, record.grid_crs,
                 record.grid_resolution_m, record.footprint_wkb, record.integrity_mode, Jsonb(record.document),
                 import_id))
            inserted += 1
        _close_journal(cursor, import_id, len(records), inserted, existing, aggregate)
        if not apply:
            raise psycopg.Rollback()
    return ObservationImportReport("acquisitions", apply, len(records), inserted, existing, aggregate,
                                   import_id if apply else None)


# --- Indicators -----------------------------------------------------------------------------------


def result_row(result: IndicatorResult) -> dict:
    """Columns of ``geonaf.indicator_results`` for one result, except import_id."""
    indicator = result.indicator
    dates, source = indicator.dates, indicator.source
    return {
        "result_sha256": result.result_sha256, "definition_sha256": result.definition_sha256,
        "geometry_sha256": result.geometry_sha256, "aoi_id": indicator.aoi_id,
        "aoi_file_sha256": indicator.aoi_sha256, "status": indicator.status, "value": indicator.value,
        "unit": indicator.unit,
        "observation_start": dates.observation_start_utc if dates else None,
        "observation_end": dates.observation_end_utc if dates else None,
        "reference_label": dates.reference_label if dates else None,
        "ingested_at": dates.ingested_at_utc if dates else None,
        "source_id": source.source_id if source else None, "source_product": source.product if source else None,
        "source_version": source.version if source else None,
        "coverage_percent": indicator.coverage_percent, "reliability": indicator.reliability,
        "computed_at": datetime.fromisoformat(result.computed_at_utc), "code_version": result.code_version,
        "result_document": result.document,
    }


def apply_indicator_import(connection, results: Iterable[IndicatorResult], *, source_label: str,
                           code_version: str, apply: bool = False) -> ObservationImportReport:
    """Insert missing definitions and results; the geometries must already be in the database."""
    import psycopg
    from psycopg.types.json import Jsonb

    results = sorted(results, key=lambda r: r.result_sha256)
    if len({r.result_sha256 for r in results}) != len(results):
        raise ObservationImportError("Résultat présent deux fois dans le lot.")
    aggregate = aggregate_sha256(r.result_sha256 for r in results)
    inserted = existing = 0
    with connection.transaction(), connection.cursor() as cursor:
        import_id = _open_journal(cursor, "indicators", source_label, code_version)
        wanted = sorted({r.geometry_sha256 for r in results})
        cursor.execute("SELECT geometry_sha256 FROM geonaf.geometries WHERE geometry_sha256 = ANY(%s)", (wanted,))
        missing = sorted(set(wanted) - {row[0] for row in cursor.fetchall()})
        if missing:
            raise ObservationImportError("Géométries absentes de la base (importer d'abord le registre de "
                                         "territoires) : " + ", ".join(sha[:12] for sha in missing))
        for result in results:
            definition = result.document["definition"]
            cursor.execute(
                "INSERT INTO geonaf.indicator_definitions (definition_sha256, indicator_id, label, unit, method, "
                "result_kind, scientific_status, profile_fingerprint, definition_document) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (definition_sha256) DO NOTHING",
                (result.definition_sha256, definition["indicator_id"], definition["label"], definition["unit"],
                 definition["method"], definition["result_kind"], definition["scientific_status"],
                 definition["profile_fingerprint"], Jsonb(definition)))
            cursor.execute("SELECT definition_document FROM geonaf.indicator_definitions WHERE definition_sha256 = %s",
                           (result.definition_sha256,))
            if cursor.fetchone()[0] != definition:
                raise ObservationImportError(f"Divergence avec la base : définition {result.definition_sha256[:12]}.")
            cursor.execute("SELECT result_document FROM geonaf.indicator_results WHERE result_sha256 = %s",
                           (result.result_sha256,))
            found = cursor.fetchone()
            if found:
                stored = found[0]
                if (stored.get("indicator"), stored.get("geometry_sha256")) != (
                        result.document["indicator"], result.geometry_sha256):
                    raise ObservationImportError(f"Divergence avec la base : résultat {result.result_sha256[:12]}.")
                existing += 1                  # Same result computed again: the first record is kept.
                continue
            row = result_row(result)
            row.update(result_document=Jsonb(row["result_document"]), import_id=import_id)
            columns = list(row)
            cursor.execute(f"INSERT INTO geonaf.indicator_results ({', '.join(columns)}) "
                           f"VALUES ({', '.join(f'%({c})s' for c in columns)})", row)
            inserted += 1
        _close_journal(cursor, import_id, len(results), inserted, existing, aggregate)
        if not apply:
            raise psycopg.Rollback()
    return ObservationImportReport("indicators", apply, len(results), inserted, existing, aggregate,
                                   import_id if apply else None)


# --- Read ----------------------------------------------------------------------------------------

QUERIES = {
    "manifests": "SELECT manifest_sha256, manifest_document FROM geonaf.acquisition_manifests "
                 "ORDER BY acquired_at, product_id, ingested_at",
    "manifests_intersecting": "SELECT m.manifest_sha256, m.manifest_document FROM geonaf.acquisitions_latest AS l "
                              "JOIN geonaf.acquisition_manifests AS m ON m.manifest_sha256 = l.manifest_sha256 "
                              "WHERE ST_Intersects(l.footprint, ST_MakeEnvelope(%(west)s, %(south)s, %(east)s, "
                              "%(north)s, 4326)) ORDER BY l.acquired_at, l.product_id",
    "results": "SELECT result_sha256, result_document FROM geonaf.indicator_results "
               "WHERE (%(geometry_sha256)s::text IS NULL OR geometry_sha256 = %(geometry_sha256)s::text) "
               "ORDER BY result_sha256",
}


class _Reader:
    def __init__(self, connect=None, *, sessions=None):
        self._connect, self._sessions = connect, sessions

    def _rows(self, query: str, params: dict) -> list[dict]:
        from psycopg.rows import dict_row

        transaction = self._sessions.read() if self._sessions is not None else plain_read_transaction(self._connect)
        with transaction as connection, connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(QUERIES[query], params)
            return cursor.fetchall()


def _acquisition(row) -> AcquisitionRecord:
    record = acquisition_record(dict(row["manifest_document"]))
    if record.manifest_sha256 != row["manifest_sha256"]:
        raise ValueError("Manifeste d'acquisition modifié dans la base depuis son import.")
    return record


class PostgisAcquisitionCatalog(_Reader):
    """Same read methods as :class:`src.data.observations.FileAcquisitionCatalog`."""

    def scan(self) -> AcquisitionScan:
        return AcquisitionScan(tuple(_acquisition(row) for row in self._rows("manifests", {})), ())

    def latest(self) -> tuple[AcquisitionRecord, ...]:
        return self.scan().latest()

    def intersecting(self, wgs84_bounds) -> tuple[AcquisitionRecord, ...]:
        west, south, east, north = (float(v) for v in wgs84_bounds)
        rows = self._rows("manifests_intersecting", {"west": west, "south": south, "east": east, "north": north})
        return tuple(_acquisition(row) for row in rows)


class PostgisIndicatorStore(_Reader):
    """Same read method as :class:`src.data.observations.IndicatorResultStore` (no write: imports only)."""

    def results(self, geometry_sha256: str | None = None) -> tuple[IndicatorResult, ...]:
        output = [result_from_envelope(dict(row["result_document"]), row["result_sha256"])
                  for row in self._rows("results", {"geometry_sha256": geometry_sha256})]
        return tuple(sorted(output, key=lambda r: (r.indicator.indicator_id, r.computed_at_utc, r.result_sha256)))

    def save(self, *_, **__):
        raise PermissionError("Stockage PostGIS en lecture seule dans l'application : importer depuis les fichiers.")
