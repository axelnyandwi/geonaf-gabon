"""Post-rename verification 10_verify_legacy_schema_rename.sql (D2.9d).

Static part (always run): the structure rows of the verification are those of the export D query, character for
character, so that the fingerprint compares with export D.
Database part (marker ``postgis``): on a LOCAL throwaway PostgreSQL only (``GEONAF_TEST_DATABASE_URL``, database
name ending with ``_test``; the geonaf and geonaf_legacy_draft schemas are dropped and recreated there). The
rename is done directly here with the mark of 09, so these tests never depend on the working copy of
09_rename_legacy_schema_application.sql (whose confirmation line may hold a value typed for a real run).
"""

import os
from pathlib import Path
import re

import pytest

ROOT = Path(__file__).resolve().parents[1]
VERIFY = ROOT / "db" / "supabase" / "10_verify_legacy_schema_rename.sql"
STRUCTURE = ROOT / "scripts" / "db_backup" / "legacy_structure.sql"
TEST_URL = os.environ.get("GEONAF_TEST_DATABASE_URL", "").strip()
GEONAF_TEST_OID = "18655::oid"
GEONAF_TEST_SHA = "'e00810f8b82477537aad150a90edf19f370a5c8e5d705f1e659079984649505c'::text"
MARK = ("geonaf-legacy: schema herite trouve le 2026-11-03, renomme avant la migration 0001, non utilise"
        " ; commentaire initial (JSON) : null")
LEGACY = ("acquisitions", "analysis_acquisitions", "analysis_runs", "indicator_definitions", "indicator_results",
          "sources", "territories", "territory_versions")
STATE = """
SELECT string_agg(n.oid || ':' || n.nspname || '=' || coalesce(obj_description(n.oid, 'pg_namespace'), '-') || ':' ||
       coalesce((SELECT string_agg(relname, ',' ORDER BY relname) FROM pg_class WHERE relnamespace = n.oid), ''),
       ' | ' ORDER BY n.nspname)
FROM pg_namespace n WHERE n.nspname NOT LIKE 'pg\\_%' AND n.nspname <> 'information_schema'
"""


# --- Static ------------------------------------------------------------------------------------------------

def _rows_after_schema_line(sql: str) -> str:
    """Union branches from the relation rows to the trigger rows (the schema line differs by design)."""
    start = sql.index("  SELECT 'relation', r.relname,")
    end = sql.index("WHERE NOT t.tgisinternal", start) + len("WHERE NOT t.tgisinternal")
    return sql[start:end]


def test_verification_rebuilds_the_rows_of_export_d_character_for_character():
    verify, structure = VERIFY.read_text(encoding="utf-8"), STRUCTURE.read_text(encoding="utf-8")
    assert _rows_after_schema_line(verify) == _rows_after_schema_line(structure)
    for same in ("string_agg(kind || '|' || name || '|' || detail, E'\\n' ORDER BY kind COLLATE \"C\", name COLLATE \"C\")",
                 "encode(sha256(convert_to(coalesce(", "SELECT set_config('search_path', '', true);"):
        assert same in verify and same in structure, same
    # The only rewrites: the qualified prefix of the new name, and the schema comment taken from the mark.
    assert verify.count("regexp_replace(") == 1
    assert "regexp_replace(detail, '\\mgeonaf_legacy_draft\\.', 'geonaf.', 'g')" in verify
    assert "coalesce((SELECT json FROM initial), 'marque illisible') AS detail" in verify


def test_verification_holds_the_geonaf_test_references_once():
    verify = VERIFY.read_text(encoding="utf-8")
    assert verify.count(GEONAF_TEST_OID) == 1 and verify.count(GEONAF_TEST_SHA) == 1
    assert "'GO'" in verify and "'NO GO'" in verify
    code = re.sub(r"'(?:[^']|'')*'", "''", re.sub(r"--[^\n]*", "", verify))     # Literals hold "; " too.
    statements = [s for s in code.split(";") if s.strip()]
    assert len(statements) == 3            # Read-only guard, empty search path, one SELECT.


# --- Local database -----------------------------------------------------------------------------------------

needs_db = [pytest.mark.postgis,
            pytest.mark.skipif(not TEST_URL, reason="GEONAF_TEST_DATABASE_URL non défini : base locale requise.")]


def conninfo() -> str:
    return "postgresql://" + TEST_URL.split("://", 1)[1] if TEST_URL.startswith("postgresql+psycopg://") else TEST_URL


def run(c, sql: str):
    cursor = c.execute(sql.encode("utf-8"))
    rows = cursor.fetchall() if cursor.description else None
    while cursor.nextset():
        rows = cursor.fetchall() if cursor.description else rows
    return rows


@pytest.fixture
def db():
    """Autocommit connection on a legacy schema shaped like geonaf-test: 8 empty tables, RLS on, no policy."""
    psycopg = pytest.importorskip("psycopg")
    from psycopg.conninfo import conninfo_to_dict

    info = conninfo_to_dict(conninfo())
    if not str(info.get("dbname", "")).endswith("_test") or info.get("host") not in ("127.0.0.1", "localhost", "::1"):
        pytest.fail("Base locale dont le nom se termine par « _test » exigée.")
    with psycopg.connect(conninfo(), autocommit=True) as c:
        for schema in ("geonaf", "geonaf_legacy_draft", "geonaf_autre"):
            c.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        c.execute("DROP VIEW IF EXISTS public.lit_legacy")
        c.execute("DROP FUNCTION IF EXISTS public.cite_geonaf()")
        c.execute("CREATE SCHEMA geonaf")
        for table in LEGACY:
            c.execute(f"CREATE TABLE geonaf.{table} (id bigint GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY, "
                      f"label text NOT NULL DEFAULT 'x' CHECK (label <> ''), created_at timestamptz DEFAULT now())")
            c.execute(f"ALTER TABLE geonaf.{table} ENABLE ROW LEVEL SECURITY")
        c.execute("ALTER TABLE geonaf.territory_versions ADD COLUMN territory_id bigint REFERENCES geonaf.territories(id)")
        c.execute("CREATE INDEX territory_versions_territory ON geonaf.territory_versions (territory_id)")
        c.execute("COMMENT ON COLUMN geonaf.sources.label IS 'libellé'")
        yield c
        for schema in ("geonaf", "geonaf_legacy_draft", "geonaf_autre"):
            c.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        c.execute("DROP VIEW IF EXISTS public.lit_legacy")
        c.execute("DROP FUNCTION IF EXISTS public.cite_geonaf()")


def export_d(c) -> tuple[str, int]:
    """Fingerprint of export D (legacy_structure.sql) on the schema before its rename."""
    rows = run(c, STRUCTURE.read_text(encoding="utf-8"))
    (digest,) = [r for r in rows if r[0] == "empreinte"]
    return digest[3], int(digest[2])


def rename(c, mark: str = MARK, target: str = "geonaf_legacy_draft"):
    """The two statements of 09 application, run directly (same names, same mark)."""
    from psycopg import sql

    c.execute(sql.SQL("ALTER SCHEMA geonaf RENAME TO {}").format(sql.Identifier(target)))
    c.execute(sql.SQL("COMMENT ON SCHEMA {} IS {}").format(sql.Identifier(target), sql.Literal(mark)))


def verify(c, oid: int, sha: str) -> dict:
    sql = VERIFY.read_text(encoding="utf-8")
    assert sql.count(GEONAF_TEST_OID) == 1 and sql.count(GEONAF_TEST_SHA) == 1
    sql = sql.replace(GEONAF_TEST_OID, f"{oid}::oid").replace(GEONAF_TEST_SHA, f"'{sha}'::text")
    return {r[0]: r for r in run(c, sql)}


def legacy_oid(c) -> int:
    return c.execute("SELECT to_regnamespace('geonaf')::oid::int").fetchone()[0]


def non_conforming(result: dict) -> set:
    return {order for order, row in result.items() if row[4] == "NON CONFORME"}


def db_test(function):
    for marker in needs_db:
        function = marker(function)
    return function


@db_test
def test_before_the_rename_the_verdict_is_no_go(db):
    sha, _ = export_d(db)
    result = verify(db, legacy_oid(db), sha)
    assert result[99][3] == "NO GO"
    assert {3, 4, 5, 6, 7, 8, 10, 12, 14} <= non_conforming(result)


@db_test
def test_after_the_rename_every_check_passes_and_the_structure_equals_export_d(db):
    oid = legacy_oid(db)
    sha, count = export_d(db)
    rename(db)
    result = verify(db, oid, sha)
    assert non_conforming(result) == set(), {k: v for k, v in result.items() if v[4] == "NON CONFORME"}
    assert result[99][3] == "GO" and result[99][4] == "0 controle(s) bloquant(s) non conforme(s)"
    assert result[10][3] == sha and result[11][3] == str(count) and result[5][3] == str(oid)
    assert result[8][3] == ",".join(LEGACY) and result[9][3] == "0" and result[16][3] == "8 / 0"
    # Same OID, new name, exact mark: the objects were kept, not recreated.
    assert db.execute("SELECT oid::int, obj_description(oid, 'pg_namespace') FROM pg_namespace "
                      "WHERE nspname = 'geonaf_legacy_draft'").fetchone() == (oid, MARK)


@db_test
def test_the_verification_writes_nothing(db):
    sha, _ = export_d(db)
    oid = legacy_oid(db)
    rename(db)
    before = db.execute(STATE).fetchone()[0]
    verify(db, oid, sha)
    assert db.execute(STATE).fetchone()[0] == before
    sql = VERIFY.read_text(encoding="utf-8")
    with pytest.raises(Exception):                            # A write slipped into the request would be refused.
        run(db, sql.replace("WITH attendu AS (", "WITH w AS (INSERT INTO geonaf_legacy_draft.sources DEFAULT VALUES RETURNING 1), attendu AS (", 1))


@db_test
def test_a_missing_table_is_detected(db):
    oid, (sha, _) = legacy_oid(db), export_d(db)
    rename(db)
    db.execute("DROP TABLE geonaf_legacy_draft.analysis_runs CASCADE")
    result = verify(db, oid, sha)
    assert result[99][3] == "NO GO" and {8, 10} <= non_conforming(result)


@pytest.mark.parametrize("change", [
    "ALTER TABLE geonaf_legacy_draft.sources ALTER COLUMN label TYPE varchar(200)",
    "ALTER TABLE geonaf_legacy_draft.sources ALTER COLUMN label DROP DEFAULT",
    "ALTER TABLE geonaf_legacy_draft.sources ADD COLUMN extra int",
    "CREATE INDEX extra_index ON geonaf_legacy_draft.sources (created_at)",
    "ALTER TABLE geonaf_legacy_draft.sources DISABLE ROW LEVEL SECURITY",
    "CREATE POLICY lecture ON geonaf_legacy_draft.sources FOR SELECT USING (true)",
    "COMMENT ON TABLE geonaf_legacy_draft.sources IS 'modifiee'",
    "ALTER TABLE geonaf_legacy_draft.territory_versions DROP CONSTRAINT territory_versions_territory_id_fkey",
])
@db_test
def test_any_structure_change_is_detected(db, change):
    oid, (sha, _) = legacy_oid(db), export_d(db)
    rename(db)
    db.execute(change)
    result = verify(db, oid, sha)
    assert result[99][3] == "NO GO" and 10 in non_conforming(result)


@db_test
def test_an_unexpected_oid_is_detected(db):
    oid, (sha, _) = legacy_oid(db), export_d(db)
    rename(db)
    result = verify(db, oid + 1, sha)
    assert result[99][3] == "NO GO" and non_conforming(result) == {5}


@pytest.mark.parametrize("mark, failing", [
    ("geonaf-legacy: autre texte", {6, 7, 10}),
    (MARK.replace(": null", ': "un commentaire"'), {6, 7, 10}),
    ("geonaf-environment:test", {6, 7, 10}),
])
@db_test
def test_an_incorrect_mark_is_detected(db, mark, failing):
    oid, (sha, _) = legacy_oid(db), export_d(db)
    rename(db, mark=mark)
    result = verify(db, oid, sha)
    assert result[99][3] == "NO GO" and non_conforming(result) == failing


@db_test
def test_a_schema_renamed_to_another_name_is_not_accepted(db):
    oid, (sha, _) = legacy_oid(db), export_d(db)
    rename(db, target="geonaf_autre")
    result = verify(db, oid, sha)
    assert result[99][3] == "NO GO" and {4, 5, 6, 8, 10} <= non_conforming(result)


@pytest.mark.parametrize("setup, failing", [
    ("CREATE VIEW public.lit_legacy AS SELECT id FROM geonaf_legacy_draft.sources", {19}),
    ("CREATE FUNCTION public.cite_geonaf() RETURNS int LANGUAGE sql AS 'SELECT 1 /* geonaf */'", {20}),
])
@db_test
def test_references_from_outside_are_detected(db, setup, failing):
    oid, (sha, _) = legacy_oid(db), export_d(db)
    rename(db)
    db.execute(setup)
    result = verify(db, oid, sha)
    assert result[99][3] == "NO GO" and non_conforming(result) == failing


@db_test
def test_grants_to_api_roles_are_detected(db):
    oid, (sha, _) = legacy_oid(db), export_d(db)
    rename(db)
    db.execute("GRANT USAGE ON SCHEMA geonaf_legacy_draft TO PUBLIC")
    db.execute("GRANT SELECT ON geonaf_legacy_draft.sources TO PUBLIC")
    result = verify(db, oid, sha)
    assert result[99][3] == "NO GO" and non_conforming(result) == {15} and result[15][3] == "2"


@db_test
def test_rows_are_counted_where_the_storage_is_not_empty(db):
    oid, (sha, _) = legacy_oid(db), export_d(db)
    rename(db)
    db.execute("INSERT INTO geonaf_legacy_draft.sources (label) VALUES ('ligne')")
    try:
        db.execute("SELECT xmlparse(content '<a/>')")
    except Exception:          # noqa: BLE001
        pytest.skip("Serveur local sans libxml : comptage exact non testable ici (Supabase dispose de libxml).")
    result = verify(db, oid, sha)
    assert result[99][3] == "NO GO" and non_conforming(result) == {9} and result[9][3] == "1"
