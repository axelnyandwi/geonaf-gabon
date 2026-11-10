"""Full SQL Editor 02 -> 03 on real PostGIS, using the existing isolated-test capability.

Only documented operator confirmations and the line endings inside expected view literals
are varied. No structural, history or identity guard is disabled. No Supabase connection is accepted.
"""
import os
from pathlib import Path
import re

import pytest

from tests.postgis_support import isolated_connection
from tests.test_supabase_legacy_rename_integration import fingerprint, run

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "db/supabase"
pytestmark = pytest.mark.postgis


def script(name):
    return (SCRIPTS / name).read_text(encoding="utf-8")


@pytest.fixture
def local_db():
    url = os.environ.get("GEONAF_TEST_DATABASE_URL", "").strip()
    if not url:
        pytest.skip("Base locale isolee non configuree; aucune connexion tentee.")
    pytest.importorskip("psycopg")
    with isolated_connection(url, autocommit=True) as c:
        assert c.execute("SELECT to_regnamespace('geonaf')").fetchone()[0] is None
        try:
            yield c
        finally:
            c.execute("DROP SCHEMA IF EXISTS geonaf CASCADE")


@pytest.fixture
def after_0001(local_db):
    extension = local_db.execute("SELECT n.nspname FROM pg_extension e JOIN pg_namespace n ON n.oid=e.extnamespace WHERE e.extname='postgis'").fetchone()
    if extension != ("extensions",):
        pytest.skip("Parcours SQL Editor exige PostGIS reel dans extensions.")
    run(local_db, script("02_migration_0001_application.sql"))
    assert environment(local_db) is None
    return local_db


def environment(c):
    return c.execute("SELECT obj_description(to_regnamespace('geonaf'),'pg_namespace')").fetchone()[0]


def confirmed(c):
    # The fixture has already checked the independent DATABASE comment and local capability.
    # These local confirmations model the operator's documented I2 + continuity input;
    # they do not attest any Supabase dashboard identity.
    start = c.execute("SELECT to_char(pg_postmaster_start_time() AT TIME ZONE 'UTC','YYYY-MM-DD HH24:MI:SS.US')").fetchone()[0]
    text = script("03_mark_environment_test.sql")
    assert text.count("confirmation_i2 constant boolean := false;") == 1
    assert text.count("'A_COMPLETER_DEPUIS_11'") == 1
    return text.replace("confirmation_i2 constant boolean := false;", "confirmation_i2 constant boolean := true;").replace("'A_COMPLETER_DEPUIS_11'", "'" + start + "'")


def with_view_reference_line_endings(text, ending):
    """Model transport conversion inside the two view literals, without changing other guards."""
    pattern = r"(WITH expected(?:_raw)?\(view_name, definition\) AS \(VALUES\n)(.*?)(\n    \),)"
    match = re.search(pattern, text, re.S)
    assert match is not None
    literals = re.findall(r"'(?:[^']|'')*'", match[2])
    assert len(literals) == 4  # Two names and two definitions.
    changed = re.sub(r"'(?:[^']|'')*'",
                     lambda m: m[0].replace("\r\n", "\n").replace("\n", ending), match[2])
    return text[:match.start(2)] + changed + text[match.end(2):]


def refused_view_with_both_reference_endings(c):
    for ending in ("\n", "\r\n"):
        text = with_view_reference_line_endings(confirmed(c), ending)
        refused_unchanged(c, text, "vues post-0001 non conformes")
        assert environment(c) is None


def snapshot(c):
    from psycopg import sql
    catalog = fingerprint(c)  # Existing read-only SHA-256 catalog mechanism.
    rows = []
    for (name,) in c.execute("SELECT relname FROM pg_class WHERE relnamespace=to_regnamespace('geonaf') AND relkind='r' ORDER BY relname").fetchall():
        data = c.execute(sql.SQL("SELECT to_jsonb(t)::text FROM geonaf.{} t ORDER BY 1").format(sql.Identifier(name))).fetchall()
        rows.append((name, data))
    views = c.execute("SELECT relname, pg_get_viewdef(oid,false) FROM pg_class WHERE relnamespace=to_regnamespace('geonaf') AND relkind='v' ORDER BY relname").fetchall()
    return catalog, rows, views


def refused_unchanged(c, text, message):
    import psycopg
    before = snapshot(c)
    with pytest.raises(psycopg.Error, match=message):
        run(c, text)
    assert snapshot(c) == before


def test_0001_then_03_succeeds_on_independently_isolated_postgis(after_0001):
    c = after_0001
    before = c.execute("SELECT version,body_sha256,applied_at,applied_by,channel FROM geonaf.schema_migrations").fetchall()
    for ending in ("\n", "\r\n"):
        # Each transport variant must succeed from an unmarked post-0001 schema.
        with c.transaction(force_rollback=True):
            assert environment(c) is None
            text = with_view_reference_line_endings(confirmed(c), ending)
            for _ in range(2):  # Same initial data and mark: repeat remains accepted.
                run(c, text)
                assert environment(c) == "geonaf-environment:test"
                assert c.execute("SELECT version,body_sha256,applied_at,applied_by,channel FROM geonaf.schema_migrations").fetchall() == before
        assert environment(c) is None


def test_s0_cannot_become_test_even_with_operator_confirmations(local_db):
    c = local_db
    c.execute("CREATE SCHEMA geonaf")
    for name in ("acquisitions", "analysis_acquisitions", "analysis_runs", "indicator_definitions", "indicator_results", "sources", "territories", "territory_versions"):
        c.execute("CREATE TABLE geonaf." + name + " (id integer PRIMARY KEY)")
    c.execute("INSERT INTO geonaf.territories VALUES (7)")
    refused_unchanged(c, confirmed(c), "schema historique ou incomplet refuse")
    assert environment(c) is None


def test_missing_constraint_is_refused_without_writing(after_0001):
    c = after_0001
    c.execute("ALTER TABLE geonaf.registry_imports DROP CONSTRAINT registry_imports_records_seen_check")
    refused_unchanged(c, confirmed(c), "contraintes post-0001 non conformes")


def test_changed_check_with_same_name_and_count_is_refused_without_writing(after_0001):
    c = after_0001
    c.execute("ALTER TABLE geonaf.registry_imports DROP CONSTRAINT registry_imports_records_seen_check, ADD CONSTRAINT registry_imports_records_seen_check CHECK (records_seen >= -1)")
    refused_unchanged(c, confirmed(c), "contraintes post-0001 non conformes")


def test_changed_foreign_key_is_refused_without_writing(after_0001):
    c = after_0001
    c.execute("ALTER TABLE geonaf.legal_statuses DROP CONSTRAINT legal_statuses_kind_fkey, ADD CONSTRAINT legal_statuses_kind_fkey FOREIGN KEY (kind) REFERENCES geonaf.territory_kinds(kind) ON DELETE CASCADE")
    refused_unchanged(c, confirmed(c), "contraintes post-0001 non conformes")


def test_changed_primary_key_order_is_refused_without_writing(after_0001):
    c = after_0001
    c.execute("ALTER TABLE geonaf.territory_versions DROP CONSTRAINT territory_versions_pkey, ADD CONSTRAINT territory_versions_pkey PRIMARY KEY (version,territory_id)")
    refused_unchanged(c, confirmed(c), "contraintes post-0001 non conformes")


def test_missing_view_is_refused_without_writing(after_0001):
    c = after_0001
    c.execute("DROP VIEW geonaf.territory_identities")
    refused_view_with_both_reference_endings(c)


def test_changed_view_filter_is_refused_without_writing(after_0001):
    c = after_0001
    c.execute("CREATE OR REPLACE VIEW geonaf.territory_identities WITH (security_invoker=true) AS SELECT territory_id,version,kind,geometry_sha256,file_sha256,record_fingerprint FROM geonaf.territory_versions WHERE version > 1")
    refused_view_with_both_reference_endings(c)


def test_changed_latest_view_order_is_refused_without_writing(after_0001):
    c = after_0001
    c.execute("CREATE OR REPLACE VIEW geonaf.territories_latest WITH (security_invoker=true) AS SELECT DISTINCT ON (territory_id) * FROM geonaf.territory_versions ORDER BY territory_id, version ASC")
    refused_view_with_both_reference_endings(c)


def test_changed_view_security_is_refused_without_writing(after_0001):
    c = after_0001
    c.execute("ALTER VIEW geonaf.territory_identities SET (security_invoker=false)")
    refused_view_with_both_reference_endings(c)


def test_history_forgery_is_refused_without_writing(after_0001):
    c = after_0001
    # Owner tampers with the history, then restores its triggers; the hash must still detect it.
    c.execute("ALTER TABLE geonaf.schema_migrations DISABLE TRIGGER schema_migrations_immutable")
    c.execute("UPDATE geonaf.schema_migrations SET body_sha256=repeat('0',64)")
    c.execute("ALTER TABLE geonaf.schema_migrations ENABLE TRIGGER schema_migrations_immutable")
    refused_unchanged(c, confirmed(c), "historique 0001 absent ou incoherent")


def test_unconfirmed_target_and_stale_instance_are_refused_without_writing(after_0001):
    c = after_0001
    refused_unchanged(c, script("03_mark_environment_test.sql"), "confirmation I2 privee")
    stale = confirmed(c)
    start = c.execute("SELECT to_char(pg_postmaster_start_time() AT TIME ZONE 'UTC','YYYY-MM-DD HH24:MI:SS.US')").fetchone()[0]
    stale = stale.replace("'" + start + "'", "'1900-01-01 00:00:00.000000'")
    refused_unchanged(c, stale, "confirmation I2 privee")


def test_changed_unique_null_semantics_are_refused_without_writing(after_0001):
    from psycopg import sql
    c = after_0001
    (name,) = c.execute("SELECT conname FROM pg_constraint WHERE conrelid='geonaf.source_releases'::regclass AND contype='u'").fetchone()
    c.execute(sql.SQL("ALTER TABLE geonaf.source_releases DROP CONSTRAINT {}, ADD CONSTRAINT {} UNIQUE (source_id,product,version,licence_id,attribution,url,share_alike,reference_year)").format(sql.Identifier(name),sql.Identifier(name)))
    refused_unchanged(c, confirmed(c), "contraintes post-0001 non conformes")


def test_unvalidated_constraint_is_refused_without_writing(after_0001):
    c = after_0001
    c.execute("ALTER TABLE geonaf.registry_imports DROP CONSTRAINT registry_imports_records_seen_check, ADD CONSTRAINT registry_imports_records_seen_check CHECK (records_seen >= 0) NOT VALID")
    refused_unchanged(c, confirmed(c), "contraintes post-0001 non conformes")


def test_independent_local_database_identity_is_required(local_db, monkeypatch):
    from uuid import uuid4
    from src.data.database import EnvironmentIdentityError
    c = local_db
    c.execute("CREATE SCHEMA geonaf")
    c.execute("CREATE TABLE geonaf.territories (id integer)")
    before = snapshot(c)
    monkeypatch.setenv("GEONAF_TEST_INSTANCE_ID", str(uuid4()))
    with pytest.raises(EnvironmentIdentityError, match="DB_TEST_IDENTITY"):
        isolated_connection(os.environ["GEONAF_TEST_DATABASE_URL"], autocommit=True)
    assert snapshot(c) == before
    assert environment(c) is None



def test_missing_history_is_refused_without_writing(after_0001):
    c = after_0001
    c.execute("DROP TABLE geonaf.schema_migrations")
    refused_unchanged(c, confirmed(c), "schema historique ou incomplet refuse")


def test_empty_history_is_refused_without_writing(after_0001):
    c = after_0001
    c.execute("ALTER TABLE geonaf.schema_migrations DISABLE TRIGGER schema_migrations_immutable")
    c.execute("DELETE FROM geonaf.schema_migrations")
    c.execute("ALTER TABLE geonaf.schema_migrations ENABLE TRIGGER schema_migrations_immutable")
    refused_unchanged(c, confirmed(c), "historique 0001 absent ou incoherent")


def test_extra_migration_is_refused_without_writing(after_0001):
    c = after_0001
    c.execute("INSERT INTO geonaf.schema_migrations(version,body_sha256,channel) VALUES ('0002_observations',repeat('1',64),'supabase-sql-editor')")
    refused_unchanged(c, confirmed(c), "historique 0001 absent ou incoherent")
