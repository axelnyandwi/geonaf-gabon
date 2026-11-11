"""SQL for the Supabase SQL Editor (D2-a2): migration wrapper, import batches, checks; no network.

Supabase is reachable from GEONAF's workstation over HTTPS only (PostgreSQL ports closed), so neither
Alembic nor psycopg can be used. Everything is therefore produced as plain SQL text, reviewed, then
pasted by a person into the SQL Editor:

* every script is **one request without BEGIN/COMMIT**: PostgreSQL runs a multi-statement request as
  a single implicit transaction, so any error cancels everything and leaves the session clean (an
  explicit BEGIN would leave it in a failed transaction). ``00_preflight.sql`` checks that the editor
  behaves this way before anything else is run;
* the migration wrapper runs ``db/sql/0001_territorial_registry.up.sql`` **unchanged**, with PostGIS's
  schema on the search_path, and records it in ``geonaf.schema_migrations`` (the Alembic history is
  aligned later with ``alembic stamp`` once a direct connection exists). Its simulation runs the same
  statements and ends with a deliberate error;
* each import batch carries verified rows (from :func:`plan_import`, file registry only) into
  temporary tables, refuses any divergence, then inserts what is missing, in one transaction. A
  *simulation* batch ends with a deliberate error: the editor always shows it, and nothing is kept;
* the import check and the parity export are single SELECT statements (read only by construction);
  the export, saved as CSV, is compared with the file registry by :func:`check_export_rows`.

Literals are generated here, never interpolated by hand: text with doubled quotes, documents in
dollar quotes whose tag cannot occur in the content, floats with Python's round-trip ``repr``.
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Iterable, Mapping

from src.data.postgis_territory_registry import (
    ImportPlan, record_from_row, release_key, version_row,
)

ROOT = Path(__file__).resolve().parents[2]
MIGRATION_0001 = ROOT / "db" / "sql" / "0001_territorial_registry.up.sql"
MIGRATION_VERSION = "0001_territorial_registry"
MIGRATION_0002 = ROOT / "db" / "sql" / "0002_observations.up.sql"
MIGRATION_0002_VERSION = "0002_observations"
# PostGIS lives in "extensions" on Supabase, in "public" on the postgis/postgis image. Local to the
# request's transaction (set_config with is_local = true).
# R5: public is never searched (an object named like a PostGIS function or type there would be bound for
# good to the constraints of 0001); pg_temp is listed last so that a temporary table cannot shadow a
# type either, while the import batches still find their temporary staging tables.
SEARCH_PATH = "SELECT set_config('search_path', 'pg_catalog, extensions, pg_temp', true);"
ENVIRONMENT_COMMENT = "geonaf-environment:{name}"
AKANDA_BOUNDS = (9.38, 0.50, 9.50, 0.62)        # Around the Akanda acquisition square (spatial check).
# SQL types of the temporary import table, in version_row order.
VERSION_TYPES = {
    "territory_id": "text", "version": "integer", "kind": "text", "label": "text", "legal_status": "text",
    "legal_evidence": "text", "retrieved_at_utc": "text", "geometry_sha256": "text", "file_sha256": "text",
    "storage_key": "text", "wgs84_bounds": "float8[]", "area_km2": "float8", "vertices": "integer",
    "parts": "integer", "created_at_utc": "text", "country_iso3": "text", "admin_level_code": "text",
    "local_type": "text", "source_unit_id": "text", "parent_territory_id": "text", "grid_system": "text",
    "grid_cell_id": "text", "grid_cell_size_deg": "float8", "scientific_profile_id": "text", "limits": "text[]",
    "supersedes": "text", "schema": "text", "record_fingerprint": "text", "record_document": "jsonb",
}
RELEASE_TYPES = {"source_id": "text", "product": "text", "source_version": "text", "licence_id": "text",
                 "attribution": "text", "url": "text", "share_alike": "boolean", "reference_year": "integer"}
EXPORT_COLUMNS = ("territory_id", "version", "kind", "label", "legal_status", "geometry_sha256", "file_sha256",
                  "storage_key", "supersedes", "record_fingerprint", "record_document", "retrieved_at_utc",
                  "source_id", "product", "source_version", "licence_id", "attribution", "url", "share_alike",
                  "reference_year", "source_geometry_type", "wkb_sha256", "srid", "total_rows")
STORAGE_SRID = 4326                       # WGS84: storage CRS of geonaf.geometries (metric work: EPSG:32632).
SQL_EDITOR_NULL = "null"                  # How the Supabase SQL Editor writes SQL NULL in a downloaded CSV.


# --- Literals ------------------------------------------------------------------------------------


def sql_text(value: str) -> str:
    if "\x00" in value:
        raise ValueError("Caractère nul interdit dans un littéral SQL.")
    return "'" + value.replace("'", "''") + "'"


def dollar_quoted(value: str) -> str:
    """Dollar-quoted constant whose tag does not occur in the content."""
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    for length in range(8, 65, 8):
        tag = f"$g{digest[:length]}$"
        if tag not in value:
            return f"{tag}{value}{tag}"
    raise ValueError("Aucune balise de citation disponible.")


def sql_literal(value, sql_type: str) -> str:
    if value is None:
        return f"NULL::{sql_type}"
    if sql_type == "jsonb":
        return dollar_quoted(json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)) + "::jsonb"
    if sql_type == "boolean":
        if not isinstance(value, bool):
            raise TypeError("Booléen attendu.")
        return "true" if value else "false"
    if sql_type == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError("Entier attendu.")
        return str(value)
    if sql_type == "float8":
        value = float(value)
        if not math.isfinite(value):
            raise ValueError("Nombre non fini.")
        return f"{value!r}::float8"
    if sql_type == "float8[]":
        return "ARRAY[" + ", ".join(sql_literal(v, "float8") for v in value) + "]::float8[]"
    if sql_type == "text[]":
        return "ARRAY[" + ", ".join(sql_text(str(v)) for v in value) + "]::text[]" if value else "'{}'::text[]"
    if sql_type == "text":
        return sql_text(str(value))
    raise ValueError(f"Type SQL non pris en charge : {sql_type}.")


def fingerprints_sha256(fingerprints: Iterable[str]) -> str:
    """Same aggregate as ImportPlan.fingerprints_sha256 and as the SQL checks (sorted, newline-joined)."""
    return hashlib.sha256("\n".join(sorted(fingerprints)).encode("ascii")).hexdigest()


# --- Migration -----------------------------------------------------------------------------------


def simulation_ending(message: str) -> str:
    """Deliberate error closing a simulation: the request's implicit transaction is cancelled, the editor shows it."""
    return f"""-- Simulation: the deliberate error below cancels the whole request; nothing is kept.
DO $$
BEGIN
  RAISE EXCEPTION '{message}';
END $$;
"""


def migration_wrapper(body: str | None = None, *, apply: bool = True) -> str:
    """Migration 0001 for the SQL Editor: the canonical file unchanged, as one request, then recorded."""
    body = MIGRATION_0001.read_text(encoding="utf-8") if body is None else body
    mode = "APPLICATION" if apply else "SIMULATION"
    return f"""-- GEONAF - Supabase SQL Editor - migration {MIGRATION_VERSION} ({mode})
-- Generated by scripts/build_supabase_sql.py from db/sql/0001_territorial_registry.up.sql (unchanged
-- body below). Run as the postgres role of the SQL Editor, after 00_preflight.sql and 01_roles.sql.
-- One request, no BEGIN/COMMIT: any error cancels everything. {"Run once only." if apply else "Writes nothing."}
-- Body SHA-256: {hashlib.sha256(body.encode("utf-8")).hexdigest()}

{SEARCH_PATH}

-- ===== Body of db/sql/0001_territorial_registry.up.sql (unchanged) =====
{body.rstrip()}
-- ===== End of body =====

-- Migration history while Alembic cannot connect (aligned later with: alembic stamp {MIGRATION_VERSION}).
CREATE TABLE geonaf.schema_migrations (
  version     text PRIMARY KEY,
  body_sha256 char(64) NOT NULL,
  applied_at  timestamptz NOT NULL DEFAULT now(),
  applied_by  text NOT NULL DEFAULT current_user,
  channel     text NOT NULL
);
REVOKE ALL ON geonaf.schema_migrations FROM PUBLIC;
DO $$
DECLARE api_role text;
BEGIN
  FOREACH api_role IN ARRAY ARRAY['anon', 'authenticated', 'service_role'] LOOP
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = api_role) THEN
      EXECUTE 'REVOKE ALL ON geonaf.schema_migrations FROM ' || quote_ident(api_role);
    END IF;
  END LOOP;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'geonaf_reader') THEN
    GRANT SELECT ON geonaf.schema_migrations TO geonaf_reader;
    CREATE POLICY geonaf_reader_select ON geonaf.schema_migrations FOR SELECT TO geonaf_reader USING (true);
  END IF;
END $$;
ALTER TABLE geonaf.schema_migrations ENABLE ROW LEVEL SECURITY;
CREATE TRIGGER schema_migrations_immutable BEFORE UPDATE OR DELETE ON geonaf.schema_migrations
  FOR EACH ROW EXECUTE FUNCTION geonaf.forbid_change();
CREATE TRIGGER schema_migrations_no_truncate BEFORE TRUNCATE ON geonaf.schema_migrations
  FOR EACH STATEMENT EXECUTE FUNCTION geonaf.forbid_change();
INSERT INTO geonaf.schema_migrations (version, body_sha256, channel)
VALUES ('{MIGRATION_VERSION}', '{hashlib.sha256(body.encode("utf-8")).hexdigest()}', 'supabase-sql-editor');

{"SELECT version, body_sha256, applied_at, applied_by, channel FROM geonaf.schema_migrations;" if apply else
 simulation_ending("GEONAF SIMULATION DE MIGRATION REUSSIE, AUCUNE ECRITURE")}"""


def migration_0002_wrapper(body: str | None = None, *, apply: bool = True) -> str:
    """Migration 0002 for the SQL Editor: refused unless 0001 (reviewed body) is recorded and the schema marked.

    Same shape as :func:`migration_wrapper`: one request, the canonical file unchanged, recorded in
    ``geonaf.schema_migrations``; the simulation runs the same statements and ends with a deliberate error.
    """
    body = MIGRATION_0002.read_text(encoding="utf-8") if body is None else body
    body_sha = hashlib.sha256(body.encode("utf-8")).hexdigest()
    sha_0001 = hashlib.sha256(MIGRATION_0001.read_text(encoding="utf-8").encode("utf-8")).hexdigest()
    mode = "APPLICATION" if apply else "SIMULATION"
    verification = """SELECT 'tables 0002' AS controle, count(*)::text AS valeur, '6' AS attendu
  FROM pg_catalog.pg_class WHERE relnamespace = 'geonaf'::regnamespace AND relkind = 'r'
   AND relname IN ('data_imports', 'acquisitions', 'acquisition_manifests', 'indicator_result_kinds',
                   'indicator_definitions', 'indicator_results')
UNION ALL SELECT 'tables 0002 sans RLS', count(*)::text, '0'
  FROM pg_catalog.pg_class WHERE relnamespace = 'geonaf'::regnamespace AND relkind = 'r' AND NOT relrowsecurity
UNION ALL SELECT 'migrations enregistrees', string_agg(version, ', ' ORDER BY version),
                 '0001_territorial_registry, 0002_observations'
  FROM geonaf.schema_migrations;"""
    return f"""-- GEONAF - Supabase SQL Editor - migration {MIGRATION_0002_VERSION} ({mode})
-- Generated by scripts/build_supabase_sql.py from db/sql/0002_observations.up.sql (unchanged body below).
-- Run as the postgres role of the SQL Editor, on a project in state S4 of 11_state_diagnostic.sql
-- (migration 0001 recorded with its reviewed body, schema marked). One request, no BEGIN/COMMIT: any
-- error cancels everything. {"Run once only." if apply else "Writes nothing."}
-- Body SHA-256: {body_sha}

{SEARCH_PATH}

-- Preconditions, each refused explicitly (a NULL comment never passes: no three-valued NOT IN).
-- schema_migrations is read with EXECUTE so that its absence is a clear refusal, not a parse error.
DO $$
DECLARE
  mark text := obj_description(to_regnamespace('geonaf'), 'pg_namespace');
  recorded_0001 bigint;
  recorded_0002 bigint;
BEGIN
  IF to_regnamespace('geonaf') IS NULL THEN
    RAISE EXCEPTION 'GEONAF: schema geonaf absent, 0002 refusee';
  END IF;
  IF mark IS NULL THEN
    RAISE EXCEPTION 'GEONAF: schema geonaf non marque (03 a executer avant 0002)';
  END IF;
  IF mark <> 'geonaf-environment:dev' AND mark <> 'geonaf-environment:test' THEN
    RAISE EXCEPTION 'GEONAF: marque inattendue sur le schema geonaf, 0002 refusee' USING DETAIL = mark;
  END IF;
  IF to_regclass('geonaf.schema_migrations') IS NULL THEN
    RAISE EXCEPTION 'GEONAF: migration 0001 non enregistree (geonaf.schema_migrations absente), 0002 refusee';
  END IF;
  EXECUTE 'SELECT count(*) FROM geonaf.schema_migrations WHERE version = $1 AND body_sha256 = $2'
    INTO recorded_0001 USING '{MIGRATION_VERSION}', '{sha_0001}';
  IF recorded_0001 <> 1 THEN
    RAISE EXCEPTION 'GEONAF: migration 0001 absente ou differente du corps revu, 0002 refusee';
  END IF;
  EXECUTE 'SELECT count(*) FROM geonaf.schema_migrations WHERE version = $1'
    INTO recorded_0002 USING '{MIGRATION_0002_VERSION}';
  IF recorded_0002 <> 0 THEN
    RAISE EXCEPTION 'GEONAF: migration 0002 deja enregistree';
  END IF;
END $$;

-- ===== Body of db/sql/0002_observations.up.sql (unchanged) =====
{body.rstrip()}
-- ===== End of body =====

INSERT INTO geonaf.schema_migrations (version, body_sha256, channel)
VALUES ('{MIGRATION_0002_VERSION}', '{body_sha}', 'supabase-sql-editor');

{verification if apply else simulation_ending("GEONAF SIMULATION DE MIGRATION 0002 REUSSIE, AUCUNE ECRITURE")}"""


# --- Import batches ------------------------------------------------------------------------------


def _batches(plan: ImportPlan, batch_size: int) -> list[list]:
    """Records grouped by territory (all versions in one batch, in order), at most ~batch_size per batch."""
    groups: dict[str, list] = {}
    for record in plan.records:
        groups.setdefault(record.territory_id, []).append(record)
    batches, current = [], []
    for territory_id in sorted(groups):
        if current and len(current) + len(groups[territory_id]) > batch_size:
            batches.append(current)
            current = []
        current.extend(groups[territory_id])
    if current:
        batches.append(current)
    return batches


def _version_values(record) -> str:
    row = version_row(record)
    if list(row) != list(VERSION_TYPES):
        raise AssertionError("Colonnes de version inattendues.")
    source = record.source
    release = dict(zip(RELEASE_TYPES, (source.source_id, source.product, source.version, source.license_spdx,
                                       source.attribution, source.url, source.share_alike, source.reference_year)))
    values = [sql_literal(row[c], t) for c, t in VERSION_TYPES.items()]
    values += [sql_literal(release[c], t) for c, t in RELEASE_TYPES.items()]
    return "(" + ", ".join(values) + ")"


_SAME_RELEASE = ("r.source_id = i.source_id AND r.product = i.product AND r.version = i.source_version "
                 "AND r.licence_id = i.licence_id AND r.attribution = i.attribution "
                 "AND r.url IS NOT DISTINCT FROM i.url AND r.share_alike = i.share_alike "
                 "AND r.reference_year IS NOT DISTINCT FROM i.reference_year")


def import_batch(plan: ImportPlan, records: list, number: int, total: int, *, source_label: str,
                 code_version: str, apply: bool) -> str:
    if any(mark in source_label for mark in ("/", "\\", ":")):
        raise ValueError("Le libellé de source ne doit pas être un chemin.")
    shas = sorted({r.geometry_sha256 for r in records})
    batch_fp = fingerprints_sha256(r.fingerprint for r in records)
    geometries = ",\n".join(
        f"  ({sql_text(sha)}, {sql_text(plan.geometries[sha].source_geometry_type)}, "
        f"decode('{plan.geometries[sha].wkb.hex()}', 'hex'), {plan.geometries[sha].parts}, "
        f"{plan.geometries[sha].vertices})" for sha in shas)
    versions = ",\n".join("  " + _version_values(r) for r in records)
    columns = ", ".join(VERSION_TYPES)
    temp_columns = ",\n  ".join(f"{c} {t}" for c, t in {**VERSION_TYPES, **RELEASE_TYPES}.items())
    item_values = ", ".join(f"item.{c}" for c in VERSION_TYPES)
    mode = "APPLICATION" if apply else "SIMULATION"
    ending = (f"SELECT import_id, records_seen, records_inserted, records_existing, fingerprints_sha256,\n"
              f"       fingerprints_sha256 = '{batch_fp}' AS empreinte_conforme\n"
              f"FROM geonaf.registry_imports WHERE import_id = "
              f"currval(pg_get_serial_sequence('geonaf.registry_imports', 'import_id'));\n") if apply else (
        "-- Simulation: the deliberate error below cancels the whole request and shows the report.\n"
        "DO $$\nDECLARE report record;\nBEGIN\n"
        "  SELECT records_seen, records_inserted, records_existing, fingerprints_sha256 INTO report\n"
        "  FROM geonaf.registry_imports\n"
        "  WHERE import_id = currval(pg_get_serial_sequence('geonaf.registry_imports', 'import_id'));\n"
        "  RAISE EXCEPTION 'GEONAF SIMULATION REUSSIE, AUCUNE ECRITURE : % versions, % a inserer, % deja presentes, "
        f"empreinte du lot %', report.records_seen, report.records_inserted, report.records_existing,\n"
        f"    CASE WHEN report.fingerprints_sha256 = '{batch_fp}' THEN 'conforme' ELSE 'NON CONFORME' END;\n"
        "END $$;\n")
    return f"""-- GEONAF - Supabase SQL Editor - import of the territorial registry, batch {number}/{total} ({mode})
-- Generated by scripts/build_supabase_sql.py from the verified file registry (record fingerprints, file
-- digests, geometry fingerprints, MultiPolygon conversion). Do not edit by hand.
-- {len(records)} versions, {len(shas)} geometries. Batch fingerprint: {batch_fp}
-- Global fingerprint of the registry: {plan.fingerprints_sha256}
-- One request, no BEGIN/COMMIT: any error cancels everything.
-- {"Writes on success." if apply else "Never writes: ends with a deliberate error that cancels the request."}

{SEARCH_PATH}
SELECT pg_advisory_xact_lock(hashtext('geonaf.territory_import'));

DO $$
BEGIN
  IF to_regclass('geonaf.schema_migrations') IS NULL OR NOT EXISTS (
      SELECT 1 FROM geonaf.schema_migrations WHERE version = '{MIGRATION_VERSION}') THEN
    RAISE EXCEPTION 'GEONAF: migration {MIGRATION_VERSION} non appliquee';
  END IF;
END $$;

CREATE TEMP TABLE import_geometries (
  geometry_sha256 text PRIMARY KEY, source_geometry_type text NOT NULL, wkb bytea NOT NULL,
  parts integer NOT NULL, vertices integer NOT NULL
) ON COMMIT DROP;
INSERT INTO import_geometries VALUES
{geometries};

CREATE TEMP TABLE import_versions (
  {temp_columns},
  PRIMARY KEY (territory_id, version)
) ON COMMIT DROP;
INSERT INTO import_versions VALUES
{versions};

-- Refusals: nothing is written if any of them applies.
DO $$
DECLARE problem text;
BEGIN
  SELECT string_agg(DISTINCT i.licence_id, ', ') INTO problem FROM import_versions i
    LEFT JOIN geonaf.licences l ON l.licence_id = i.licence_id WHERE l.licence_id IS NULL;
  IF problem IS NOT NULL THEN
    RAISE EXCEPTION 'GEONAF: licences absentes de la base (a ajouter par migration) : %', problem;
  END IF;
  SELECT string_agg(DISTINCT i.licence_id, ', ') INTO problem FROM import_versions i
    JOIN geonaf.licences l ON l.licence_id = i.licence_id WHERE l.share_alike <> i.share_alike;
  IF problem IS NOT NULL THEN
    RAISE EXCEPTION 'GEONAF: partage a l''identique incoherent avec la licence : %', problem;
  END IF;
  SELECT string_agg(i.territory_id || ' v' || i.version, ', ') INTO problem FROM import_versions i
    JOIN geonaf.territory_versions v ON v.territory_id = i.territory_id AND v.version = i.version
    WHERE v.record_fingerprint <> i.record_fingerprint;
  IF problem IS NOT NULL THEN
    RAISE EXCEPTION 'GEONAF: divergence avec la base, autre contenu pour : %', problem;
  END IF;
  SELECT string_agg(DISTINCT i.territory_id, ', ') INTO problem FROM import_versions i
    JOIN geonaf.territories t ON t.territory_id = i.territory_id WHERE t.kind <> i.kind;
  IF problem IS NOT NULL THEN
    RAISE EXCEPTION 'GEONAF: type de territoire different dans la base : %', problem;
  END IF;
  SELECT string_agg(g.geometry_sha256, ', ') INTO problem FROM import_geometries n
    JOIN geonaf.geometries g ON g.geometry_sha256 = n.geometry_sha256
    WHERE g.source_geometry_type <> n.source_geometry_type OR g.parts <> n.parts OR g.vertices <> n.vertices
       OR ST_AsBinary(g.geom, 'NDR') <> n.wkb;
  IF problem IS NOT NULL THEN
    RAISE EXCEPTION 'GEONAF: geometrie deja presente avec un autre contenu : %', problem;
  END IF;
END $$;

INSERT INTO geonaf.data_sources (source_id)
  SELECT DISTINCT source_id FROM import_versions ON CONFLICT (source_id) DO NOTHING;
INSERT INTO geonaf.source_releases (source_id, product, version, licence_id, attribution, url, share_alike,
                                    reference_year)
  SELECT DISTINCT i.source_id, i.product, i.source_version, i.licence_id, i.attribution, i.url, i.share_alike,
         i.reference_year
  FROM import_versions i
  WHERE NOT EXISTS (SELECT 1 FROM geonaf.source_releases r WHERE {_SAME_RELEASE});
INSERT INTO geonaf.geometries (geometry_sha256, identity_version, source_geometry_type, geom, parts, vertices)
  SELECT geometry_sha256, 'geonaf-aoi-geometry-v1', source_geometry_type, ST_GeomFromWKB(wkb, 4326), parts, vertices
  FROM import_geometries ON CONFLICT (geometry_sha256) DO NOTHING;
INSERT INTO geonaf.territories (territory_id, kind)
  SELECT DISTINCT territory_id, kind FROM import_versions ON CONFLICT (territory_id) DO NOTHING;
INSERT INTO geonaf.registry_imports (source_label, code_version)
  VALUES ({sql_text(source_label)}, {sql_text(code_version)});

-- Versions in order (the version chain is checked row by row), then the journal is closed.
DO $$
DECLARE
  item record;
  journal bigint := currval(pg_get_serial_sequence('geonaf.registry_imports', 'import_id'));
  inserted integer := 0;
BEGIN
  FOR item IN
    SELECT i.*, r.release_id FROM import_versions i JOIN geonaf.source_releases r ON {_SAME_RELEASE}
    WHERE NOT EXISTS (SELECT 1 FROM geonaf.territory_versions v WHERE v.record_fingerprint = i.record_fingerprint)
    ORDER BY i.territory_id, i.version
  LOOP
    INSERT INTO geonaf.territory_versions ({columns}, release_id, import_id)
    VALUES ({item_values}, item.release_id, journal);
    inserted := inserted + 1;
  END LOOP;
  UPDATE geonaf.registry_imports SET finished_at = now(),
         records_seen = (SELECT count(*) FROM import_versions), records_inserted = inserted,
         records_existing = (SELECT count(*) FROM import_versions) - inserted,
         fingerprints_sha256 = (SELECT encode(sha256(convert_to(string_agg(record_fingerprint, E'\\n'
                                ORDER BY record_fingerprint), 'UTF8')), 'hex') FROM import_versions)
   WHERE import_id = journal;
END $$;

{ending}"""


def import_batches(plan: ImportPlan, *, source_label: str, code_version: str, apply: bool,
                   batch_size: int = 25) -> list[str]:
    batches = _batches(plan, batch_size)
    return [import_batch(plan, records, number, len(batches), source_label=source_label,
                         code_version=code_version, apply=apply)
            for number, records in enumerate(batches, start=1)]


# --- Read-only checks ----------------------------------------------------------------------------


def expected_intersecting(plan: ImportPlan, bounds=AKANDA_BOUNDS) -> list[str]:
    """Latest versions whose geometry intersects ``bounds``, computed with shapely (expected SQL answer)."""
    import shapely
    from shapely.geometry import box

    latest = {}
    for record in plan.records:
        latest[record.territory_id] = record
    rectangle = box(*bounds)
    return sorted(territory_id for territory_id, record in latest.items()
                  if shapely.from_wkb(plan.geometries[record.geometry_sha256].wkb).intersects(rectangle))


def verify_import_sql(plan: ImportPlan) -> str:
    """One SELECT comparing the database with the plan: every line must read conforme = true."""
    kinds = {}
    for record in plan.records:
        kinds[record.kind] = kinds.get(record.kind, 0) + 1
    types = {}
    for row in plan.geometries.values():
        types[row.source_geometry_type] = types.get(row.source_geometry_type, 0) + 1
    west, south, east, north = AKANDA_BOUNDS
    intersecting = ",".join(expected_intersecting(plan))
    checks = [
        ("versions de territoires", str(len(plan.records)), "SELECT count(*)::text FROM geonaf.territory_versions"),
        ("geometries", str(len(plan.geometries)), "SELECT count(*)::text FROM geonaf.geometries"),
        ("versions de source", str(len(plan.releases)), "SELECT count(*)::text FROM geonaf.source_releases"),
        ("empreinte globale des enregistrements", plan.fingerprints_sha256,
         "SELECT encode(sha256(convert_to(string_agg(record_fingerprint, E'\\n' ORDER BY record_fingerprint), "
         "'UTF8')), 'hex') FROM geonaf.territory_versions"),
        ("types de territoires", ", ".join(f"{k}={v}" for k, v in sorted(kinds.items())),
         "SELECT string_agg(kind || '=' || n, ', ' ORDER BY kind) FROM (SELECT kind, count(*) AS n "
         "FROM geonaf.territory_versions GROUP BY kind) AS k"),
        ("types geometriques d'origine", ", ".join(f"{k}={v}" for k, v in sorted(types.items())),
         "SELECT string_agg(source_geometry_type || '=' || n, ', ' ORDER BY source_geometry_type) FROM "
         "(SELECT source_geometry_type, count(*) AS n FROM geonaf.geometries GROUP BY source_geometry_type) AS t"),
        ("geometries invalides ou non MultiPolygon", "0",
         "SELECT count(*)::text FROM geonaf.geometries WHERE NOT ST_IsValid(geom) "
         "OR GeometryType(geom) <> 'MULTIPOLYGON' OR ST_SRID(geom) <> 4326"),
        ("colonnes incoherentes avec le document d'origine", "0",
         "SELECT count(*)::text FROM geonaf.territory_versions WHERE record_document ->> 'label' <> label "
         "OR record_document ->> 'kind' <> kind OR record_document ->> 'legal_status' <> legal_status "
         "OR record_document ->> 'storage_key' <> storage_key "
         "OR (record_document ->> 'area_km2')::float8 <> area_km2 "
         "OR record_document ->> 'supersedes' IS DISTINCT FROM supersedes"),
        ("journaux d'import non clotures", "0",
         "SELECT count(*)::text FROM geonaf.registry_imports WHERE finished_at IS NULL"),
        ("unites intersectant le carre Akanda (ST_Intersects)", intersecting,
         "SELECT coalesce(string_agg(v.territory_id, ',' ORDER BY v.territory_id), '') "
         "FROM geonaf.territories_latest v JOIN geonaf.geometries g ON g.geometry_sha256 = v.geometry_sha256 "
         f"WHERE ST_Intersects(g.geom, ST_MakeEnvelope({west!r}, {south!r}, {east!r}, {north!r}, 4326))"),
    ]
    parts = [f"SELECT {sql_text(label)} AS verification, {sql_text(expected)} AS attendu, ({query}) AS obtenu"
             for label, expected, query in checks]
    return ("-- GEONAF - Supabase SQL Editor - read-only check of the imported registry (search path, then one SELECT).\n"
            "-- Generated by scripts/build_supabase_sql.py with the expected values of the file registry.\n"
            "-- Every line must read conforme = true.\n"
            + SEARCH_PATH + "\n\n"
            "SELECT verification, attendu, obtenu, attendu = obtenu AS conforme FROM (\n  "
            + "\n  UNION ALL ".join(parts) + "\n) AS checks;\n")


def check_export_rows(rows: Iterable[Mapping[str, str]], plan: ImportPlan) -> list[str]:
    """Problems found in the CSV export of 07_export_parity.sql (empty list: database = file registry).

    Besides the record and geometry checks: a key exported twice, a geometry not stored in EPSG:4326 and
    a download that holds fewer (or more) rows than the query counted (total_rows) are problems too.
    """
    rows = list(rows)
    problems, seen = [], set()
    expected = {(r.territory_id, r.version): r for r in plan.records}
    nullable = {"supersedes", "retrieved_at_utc", "url", "reference_year"}
    totals = {raw.get("total_rows") for raw in rows}
    if rows and totals != {str(len(rows))}:
        problems.append(f"export incomplet ou incohérent : {len(rows)} lignes lues, total annoncé "
                        f"{', '.join(sorted(str(t) for t in totals))}")
    for raw in rows:
        # SQL NULL: an empty field, or the text "null" the Supabase SQL Editor writes in its CSV. Accepted in
        # the nullable columns only; anywhere else "null" stays a value and fails its check.
        row = {k: (None if k in nullable and v in ("", None, SQL_EDITOR_NULL) else v) for k, v in raw.items()}
        try:
            key = (row["territory_id"], int(row["version"]))
            if key in seen:
                problems.append(f"{key[0]} v{key[1]} : exporté plusieurs fois")
                continue
            seen.add(key)
            if row["srid"] != str(STORAGE_SRID):
                problems.append(f"{key[0]} v{key[1]} : SRID {row['srid']} au lieu de {STORAGE_SRID}")
            row["version"] = key[1]
            row["share_alike"] = str(row["share_alike"]).lower() in ("true", "t")
            row["reference_year"] = None if row["reference_year"] is None else int(row["reference_year"])
            row["record_document"] = json.loads(row["record_document"])
            record = record_from_row(row)
        except (KeyError, ValueError, TypeError) as error:
            problems.append(f"{raw.get('territory_id')} v{raw.get('version')} : ligne refusée ({error})")
            continue
        reference = expected.get(key)
        if reference is None:
            problems.append(f"{key[0]} v{key[1]} : absent du registre fichier")
            continue
        if record != reference or release_key(record.source) != release_key(reference.source):
            problems.append(f"{key[0]} v{key[1]} : enregistrement différent du registre fichier")
        stored = plan.geometries[reference.geometry_sha256]
        if row["source_geometry_type"] != stored.source_geometry_type:
            problems.append(f"{key[0]} v{key[1]} : type géométrique d'origine différent")
        if row["wkb_sha256"] != hashlib.sha256(stored.wkb).hexdigest():
            problems.append(f"{key[0]} v{key[1]} : géométrie (WKB) différente de la conversion locale")
    for key in sorted(set(expected) - seen):
        problems.append(f"{key[0]} v{key[1]} : absent de la base")
    return problems
