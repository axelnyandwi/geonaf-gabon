"""03 guard regressions. Static tests are offline; execution uses an explicitly isolated local DB.

No PostGIS emulation, no production guard is removed. History predicate tests exercise only
that guard, not a successful mark. A real post-0001 positive path still requires PostGIS.
"""
import os
from pathlib import Path
import re

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "db/supabase/03_mark_environment_test.sql"
SQL = SCRIPT.read_text(encoding="utf-8")
LEGACY = ("acquisitions", "analysis_acquisitions", "analysis_runs", "indicator_definitions",
          "indicator_results", "sources", "territories", "territory_versions")
SHA = "719e7e847f28404255491c0c00923f130cbd35151cd63488ed1ba3391c0bf971"


def test_mark_and_all_guards_are_one_atomic_statement():
    assert SQL.count("DO $mark$") == SQL.count("END $mark$;") == 1
    write = SQL.index("  COMMENT ON SCHEMA geonaf")
    assert SQL.index("LOCK TABLE") < SQL.index("INTO history_ok") < write < SQL.index("END $mark$;")
    assert SQL.index("confirmation_i2 IS DISTINCT FROM true") < write
    assert "confirmation_i2 constant boolean := false" in SQL
    assert "confirmation_instance constant text := 'A_COMPLETER_DEPUIS_11'" in SQL
    assert "system_identifier" not in SQL and "app.settings" not in SQL
    assert "USING DETAIL = current_mark" not in SQL
    assert SHA in SQL and "count(*) = 1" in SQL


def test_columns_include_every_reviewed_table_and_distinguish_legacy():
    inventory = SQL.split("WITH expected(table_name, column_name")[1].split("), actual AS")[0]
    tables = set(re.findall(r"\('([a-z_]+)',", inventory))
    expected = set(re.findall(r"CREATE TABLE geonaf\.(\w+)",
                             (ROOT / "db/supabase/02_migration_0001_application.sql").read_text()))
    assert tables == expected and len(tables) == 10
    assert set(LEGACY) != expected
    assert "('schema_migrations', 'body_sha256', 'character(64)', true, '')" in inventory
    assert "('geometries', 'geom', 'extensions.geometry(MultiPolygon,4326)', true, '')" in inventory
    assert "('legal_statuses', 'legal_status', 'text', true, '')" in inventory
    assert "AND NOT a.attisdropped" in SQL


@pytest.fixture
def db():
    url = os.environ.get("GEONAF_TEST_DATABASE_URL", "").strip()
    if not url:
        pytest.skip("Base locale isolee non configuree; aucune connexion tentee.")
    pytest.importorskip("psycopg")
    from tests.postgis_support import isolated_connection
    with isolated_connection(url, autocommit=True) as connection:
        connection.execute("CREATE SCHEMA geonaf")
        try:
            yield connection
        finally:
            connection.execute("DROP SCHEMA geonaf CASCADE")


def mark(db):
    return db.execute("SELECT obj_description('geonaf'::regnamespace, 'pg_namespace')").fetchone()[0]


@pytest.mark.postgis
@pytest.mark.parametrize("note", [None, "geonaf-environment:test", "geonaf-environment:dev"])
def test_complete_script_refuses_s0_including_historical_dev_shape_without_writing(db, note):
    import psycopg
    for table in LEGACY:
        db.execute(f"CREATE TABLE geonaf.{table} (id bigint PRIMARY KEY, label text)")
        db.execute(f"ALTER TABLE geonaf.{table} ENABLE ROW LEVEL SECURITY")
    if note is not None:
        db.execute(psycopg.sql.SQL("COMMENT ON SCHEMA geonaf IS {}").format(psycopg.sql.Literal(note)))
    before = mark(db)
    with pytest.raises(psycopg.Error, match="schema historique ou incomplet refuse|autre environnement"):
        db.execute(SQL.encode())
    assert mark(db) == before
    assert db.execute("SELECT count(*) FROM pg_class WHERE relnamespace = 'geonaf'::regnamespace AND relkind='r'").fetchone()[0] == 8


@pytest.mark.postgis
def test_complete_script_refuses_missing_schema_without_creating_it(db):
    import psycopg
    db.execute("ALTER SCHEMA geonaf RENAME TO geonaf_missing_probe")
    try:
        with pytest.raises(psycopg.Error, match="schema geonaf absent"):
            db.execute(SQL.encode())
        assert db.execute("SELECT to_regnamespace('geonaf')").fetchone()[0] is None
    finally:
        db.execute("ALTER SCHEMA geonaf_missing_probe RENAME TO geonaf")


@pytest.mark.postgis
@pytest.mark.parametrize("rows", [[], [("0001_territorial_registry", "0" * 64, "supabase-sql-editor")],
    [("0002_observations", SHA, "supabase-sql-editor")],
    [("0001_territorial_registry", SHA, "other")],
    [("0001_territorial_registry", SHA, "supabase-sql-editor"), ("0002_observations", SHA, "supabase-sql-editor")]])
def test_history_guard_refuses_empty_wrong_hash_version_channel_or_extra_rows(db, rows):
    import psycopg
    db.execute("CREATE TABLE geonaf.schema_migrations (version text, body_sha256 char(64), channel text, applied_at timestamptz DEFAULT now(), applied_by text DEFAULT current_user)")
    for row in rows:
        db.execute("INSERT INTO geonaf.schema_migrations (version,body_sha256,channel) VALUES (%s,%s,%s)", row)
    start = SQL.index("  SELECT count(*) = 1")
    end = SQL.index("  IF EXISTS (SELECT 1 FROM geonaf.registry_imports)", start)
    guard = "DO $$ DECLARE history_ok boolean; BEGIN " + SQL[start:end] + " END $$;"
    with pytest.raises(psycopg.Error, match="historique 0001 absent ou incoherent"):
        db.execute(guard.encode())
    assert mark(db) is None


@pytest.mark.postgis
def test_identity_guard_is_blocking_without_private_confirmation(db):
    import psycopg
    declarations = SQL[SQL.index("DECLARE"):SQL.index("BEGIN")]
    start = SQL.index("  IF confirmation_i2 IS DISTINCT FROM true")
    end = SQL.index("  COMMENT ON SCHEMA", start)
    guard = "DO $$ " + declarations + " BEGIN " + SQL[start:end] + " END $$;"
    with pytest.raises(psycopg.Error, match="confirmation I2 privee"):
        db.execute(guard.encode())
    assert mark(db) is None


def test_every_reviewed_index_function_and_trigger_is_checked():
    canonical = (ROOT / "db/supabase/02_migration_0001_application.sql").read_text(encoding="utf-8")
    for name in re.findall(r"CREATE (?:INDEX|TRIGGER) (\w+)", canonical):
        assert "'" + name + "'" in SQL, name
    for name in re.findall(r"CREATE FUNCTION geonaf\.(\w+)", canonical):
        assert "to_regprocedure('geonaf." + name + "()')" in SQL
    assert "NOT convalidated" in SQL and "a.attnotnull" in SQL
    assert "t.tgenabled = 'O'" in SQL


def test_cleanup_remains_blocked_until_a_legitimate_test_mark_exists():
    cleanup = (ROOT / "db/supabase/99_drop_schema_test.sql").read_text(encoding="utf-8")
    assert cleanup.index("<> 'geonaf-environment:test'") < cleanup.index("DROP SCHEMA geonaf CASCADE")
    assert cleanup.index("RAISE EXCEPTION") < cleanup.index("DROP SCHEMA geonaf CASCADE")
    assert "COMMENT ON SCHEMA" not in cleanup


@pytest.mark.postgis
def test_complete_script_refuses_s0_with_forged_0001_history(db):
    import psycopg
    for table in LEGACY:
        db.execute(f"CREATE TABLE geonaf.{table} (id bigint PRIMARY KEY)")
    db.execute("CREATE TABLE geonaf.schema_migrations (version text, body_sha256 char(64))")
    db.execute("INSERT INTO geonaf.schema_migrations VALUES (%s,%s)", ("0001_territorial_registry", SHA))
    with pytest.raises(psycopg.Error, match="schema historique ou incomplet refuse"):
        db.execute(SQL.encode())
    assert mark(db) is None
    assert db.execute("SELECT count(*) FROM geonaf.schema_migrations").fetchone()[0] == 1


@pytest.mark.postgis
def test_complete_script_refuses_matching_names_with_incompatible_columns(db):
    import psycopg
    names = ("data_sources", "geometries", "legal_statuses", "licences", "registry_imports",
             "schema_migrations", "source_releases", "territories", "territory_kinds", "territory_versions")
    for table in names:
        db.execute(f"CREATE TABLE geonaf.{table} (id bigint)")
    with pytest.raises(psycopg.Error, match="colonnes post-0001 non conformes"):
        db.execute(SQL.encode())
    assert mark(db) is None


@pytest.mark.postgis
def test_cleanup_refuses_unmarked_schema_after_a_failed_mark(db):
    import psycopg
    db.execute("CREATE TABLE geonaf.recovery_probe (id integer)")
    db.execute("INSERT INTO geonaf.recovery_probe VALUES (1)")
    cleanup = (ROOT / "db/supabase/99_drop_schema_test.sql").read_text(encoding="utf-8")
    with pytest.raises(psycopg.Error, match="suppression reservee"):
        db.execute(cleanup.encode())
    assert mark(db) is None
    assert db.execute("SELECT id FROM geonaf.recovery_probe").fetchall() == [(1,)]



def test_constraints_and_views_are_compared_by_definition_not_counts_only():
    assert "pg_catalog.pg_get_constraintdef(k.oid, false)" in SQL
    assert "pg_catalog.pg_get_viewdef(c.oid, false)" in SQL
    assert "expected(table_name, constraint_name, definition)" in SQL
    assert "expected(view_name, definition)" in SQL
