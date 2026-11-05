"""11_state_diagnostic.sql on a LOCAL throwaway PostgreSQL (marker ``postgis``); skipped without one.

Needs ``GEONAF_TEST_DATABASE_URL`` on a local database whose name ends with ``_test``: the ``geonaf`` and
``geonaf_legacy_draft`` schemas are dropped and recreated there. Never Supabase.

Proved here (PostgreSQL of the test server): the request is read only and writes nothing, and the states
reachable without PostGIS are recognised: V (no GEONAF schema), S0 (legacy schema in place), S1 (renamed and
marked), INCOHERENT (anything else). States S3 to S5 need migration 0001, hence PostGIS: they are covered by
the static test of tests/test_supabase_state_diagnostic.py only, and by the run on geonaf-test.
Destructive fixtures additionally require GEONAF_ALLOW_DESTRUCTIVE_TESTS=1, an exact
GEONAF_TEST_DATABASE_NAME and GEONAF_TEST_INSTANCE_ID matching the database comment.
See tests/postgis_support.py; a _test suffix alone never authorizes access.
"""

import os
from pathlib import Path

import pytest

from tests.postgis_support import isolated_connection

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (ROOT / "db" / "supabase" / "11_state_diagnostic.sql").read_text(encoding="utf-8")
TEST_URL = os.environ.get("GEONAF_TEST_DATABASE_URL", "").strip()
pytestmark = [pytest.mark.postgis,
              pytest.mark.skipif(not TEST_URL, reason="GEONAF_TEST_DATABASE_URL non défini : base locale requise.")]
LEGACY = ("acquisitions", "analysis_acquisitions", "analysis_runs", "indicator_definitions", "indicator_results",
          "sources", "territories", "territory_versions")
MARK = "geonaf-legacy: schema herite trouve le 2026-11-03, renomme avant la migration 0001, non utilise ; " \
       "commentaire initial (JSON) : null"



@pytest.fixture
def db():
    pytest.importorskip("psycopg")
    with isolated_connection(TEST_URL, autocommit=True) as c:
        for schema in ("geonaf", "geonaf_legacy_draft"):
            c.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        yield c
        for schema in ("geonaf", "geonaf_legacy_draft"):
            c.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")


def legacy(c, schema="geonaf"):
    c.execute(f"CREATE SCHEMA {schema}")
    for table in LEGACY:
        c.execute(f"CREATE TABLE {schema}.{table} (id bigint PRIMARY KEY, label text)")
        c.execute(f"ALTER TABLE {schema}.{table} ENABLE ROW LEVEL SECURITY")


def diagnose(c) -> dict:
    cursor = c.cursor()
    cursor.execute(SCRIPT.encode("utf-8"))
    while cursor.nextset():
        pass
    rows = cursor.fetchall()
    assert [d.name for d in cursor.description] == ["ordre", "rubrique", "element", "valeur", "comparable"]
    return {(r[1], r[2]): r for r in rows}


def state(rows) -> str:
    return rows[("etat", "etat reconnu")][3]


def test_empty_project_is_state_v_and_the_request_is_read_only(db):
    rows = diagnose(db)
    assert state(rows).startswith("V :") and rows[("execution", "lecture seule")][3] == "on"
    assert rows[("schema", "geonaf")][3] == "absent" and rows[("migrations", "geonaf.schema_migrations")][3] == "absente"


def test_legacy_schema_in_place_is_state_s0(db):
    legacy(db)
    rows = diagnose(db)
    assert state(rows).startswith("S0 :") and "08 puis simulation 09" in rows[("etat", "prochaine etape")][3]
    assert rows[("tables", "geonaf")][3] == ",".join(LEGACY)
    assert rows[("tables", "geonaf : tables sans RLS")][3] == "0"


def test_renamed_and_marked_schema_is_state_s1_and_its_structure_matches_s0(db):
    legacy(db)
    before = diagnose(db)[("tables", "geonaf : structure sha256")][3]
    db.execute("ALTER SCHEMA geonaf RENAME TO geonaf_legacy_draft")
    db.execute(f"COMMENT ON SCHEMA geonaf_legacy_draft IS '{MARK}'")
    rows = diagnose(db)
    assert state(rows).startswith("S1 :")
    assert rows[("tables", "geonaf_legacy_draft : structure sha256")][3] == before      # Schema name neutralised.
    assert rows[("schema", "geonaf_legacy_draft : commentaire")][4] is True
    assert "01_roles" in rows[("etat", "prochaine etape")][3] or "10_security_precheck" in rows[("etat", "prochaine etape")][3]


@pytest.mark.parametrize("setup", [
    "CREATE SCHEMA geonaf; CREATE SCHEMA geonaf_legacy_draft",                  # Both, nothing applied.
    "CREATE SCHEMA geonaf_legacy_draft; COMMENT ON SCHEMA geonaf_legacy_draft IS 'autre chose'",
])
def test_unexpected_combinations_are_reported_as_incoherent(db, setup):
    db.execute(setup)
    rows = diagnose(db)
    assert state(rows).startswith("INCOHERENT") and rows[("etat", "prochaine etape")][3].startswith("aucune")


def test_outside_dependencies_and_api_grants_are_counted(db):
    legacy(db)
    db.execute("CREATE OR REPLACE VIEW public.geonaf_probe_view AS SELECT id FROM geonaf.sources")
    db.execute("CREATE OR REPLACE FUNCTION public.geonaf_probe_fn() RETURNS int LANGUAGE sql "
               "SET search_path = geonaf AS 'SELECT 1'")
    try:
        rows = diagnose(db)
        assert rows[("dependances", "vues hors des schemas GEONAF qui les lisent")][3] == "1"
        assert rows[("dependances", "fonctions hors catalogue qui citent geonaf (code ou search_path)")][3] == "1"
        assert rows[("droits", "objets GEONAF accessibles a anon, authenticated, service_role ou PUBLIC")][3] \
            .endswith("PUBLIC 0")
    finally:
        db.execute("DROP VIEW IF EXISTS public.geonaf_probe_view")
        db.execute("DROP FUNCTION IF EXISTS public.geonaf_probe_fn()")


def test_the_diagnostic_writes_nothing(db):
    legacy(db)
    snapshot = "SELECT string_agg(oid::text || relname, ',' ORDER BY oid) FROM pg_class WHERE relnamespace = " \
               "'geonaf'::regnamespace"
    before = db.execute(snapshot).fetchone()[0]
    diagnose(db)
    assert db.execute(snapshot).fetchone()[0] == before
    assert db.execute("SELECT obj_description('geonaf'::regnamespace, 'pg_namespace')").fetchone()[0] is None
