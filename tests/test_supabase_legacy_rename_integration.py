"""Scripts 09 (legacy schema rename) against a real PostgreSQL (marker ``postgis``); skipped without one.

Requires ``GEONAF_TEST_DATABASE_URL`` and a database whose name ends with ``_test``: each test drops and
recreates the ``geonaf`` and ``geonaf_legacy_draft`` schemas there (an empty copy of the legacy schema seen
on 2026-11-03). Only ``psycopg`` is needed. Never point it at a Supabase project.

What is proved here, on the PostgreSQL version of the test database (D2.9):
- the multi-statement request of the SQL Editor is one implicit transaction (an error cancels it all);
- the simulation leaves the schema fingerprint unchanged, and no contiguous line range of the simulation,
  application or rollback file, selected alone, changes the database, in the initial or the renamed state;
- the application refuses to run without the confirmation, and the rollback restores the name, the OID and
  the exact initial comment (NULL or not);
- event triggers: a NOTIFY is cancelled with the simulation, a sequence increment is not (residual risk).
What is not proved: the behaviour of the Supabase SQL Editor itself, the Supabase event triggers and roles.
Destructive fixtures additionally require GEONAF_ALLOW_DESTRUCTIVE_TESTS=1, an exact
GEONAF_TEST_DATABASE_NAME and GEONAF_TEST_INSTANCE_ID matching the database comment.
See tests/postgis_support.py; a _test suffix alone never authorizes access.
"""

import os
from pathlib import Path

import pytest

from tests.postgis_support import isolated_connection

ROOT = Path(__file__).resolve().parents[1]
SUPABASE = ROOT / "db" / "supabase"
TEST_URL = os.environ.get("GEONAF_TEST_DATABASE_URL", "").strip()
pytestmark = [pytest.mark.postgis,
              pytest.mark.skipif(not TEST_URL, reason="GEONAF_TEST_DATABASE_URL non défini : tests 09 non exécutés.")]

LEGACY_TABLES = ("acquisitions", "analysis_acquisitions", "analysis_runs", "indicator_definitions",
                 "indicator_results", "sources", "territories", "territory_versions")
PLACEHOLDER = "'AAAA-MM-JJ HH:MM:SS.UUUUUU'"
SIMULATION_OK = "GEONAF SIMULATION DE RENOMMAGE REUSSIE, AUCUNE ECRITURE"
MARK_PREFIX = ("geonaf-legacy: schema herite trouve le 2026-11-03, renomme avant la migration 0001, non utilise"
               " ; commentaire initial (JSON) : ")
# Every user schema with its comment and relations: any persistent change made by a fragment shows here.
STATE = """
SELECT string_agg(n.oid || ':' || n.nspname || '=' || coalesce(obj_description(n.oid, 'pg_namespace'), '-') || ':' ||
       coalesce((SELECT string_agg(relname, ',' ORDER BY relname) FROM pg_class WHERE relnamespace = n.oid), ''),
       ' | ' ORDER BY n.nspname)
FROM pg_namespace n WHERE n.nspname NOT LIKE 'pg\\_%' AND n.nspname <> 'information_schema'
"""



def script(name: str) -> str:
    return (SUPABASE / name).read_text(encoding="utf-8")


@pytest.fixture
def connect():
    """Autocommit connection factory on an empty copy of the legacy schema (the SQL Editor runs in autocommit)."""
    pytest.importorskip("psycopg")

    def factory(autocommit=True):
        return isolated_connection(TEST_URL, autocommit=autocommit)

    with factory() as c:
        c.execute("DROP SCHEMA IF EXISTS geonaf CASCADE")
        c.execute("DROP SCHEMA IF EXISTS geonaf_legacy_draft CASCADE")
        c.execute("DROP TABLE IF EXISTS public.found, public.geonaf_probe")
        c.execute("DROP FUNCTION IF EXISTS public.geonaf_probe_fn()")
        c.execute("CREATE SCHEMA geonaf")
        for table in LEGACY_TABLES:
            c.execute(f"CREATE TABLE geonaf.{table} (id bigint PRIMARY KEY, label text)")
            c.execute(f"ALTER TABLE geonaf.{table} ENABLE ROW LEVEL SECURITY")
    yield factory
    with factory() as c:
        c.execute("DROP SCHEMA IF EXISTS geonaf CASCADE")
        c.execute("DROP SCHEMA IF EXISTS geonaf_legacy_draft CASCADE")
        c.execute("DROP FUNCTION IF EXISTS public.geonaf_probe_fn()")


def run(connection, sql: str):
    """Send the text as one simple-protocol request, like the SQL Editor; return the last result rows."""
    cursor = connection.execute(sql.encode("utf-8"))
    rows = cursor.fetchall() if cursor.description else None
    while cursor.nextset():
        rows = cursor.fetchall() if cursor.description else rows
    return rows


def run_error(connection, sql: str) -> tuple[str, str]:
    import psycopg

    with pytest.raises(psycopg.Error) as caught:
        run(connection, sql)
    return caught.value.diag.message_primary or "", caught.value.diag.message_detail or ""


def fingerprint(connection) -> dict:
    cursor = connection.cursor()
    cursor.execute(script("09_legacy_schema_fingerprint.sql").encode("utf-8"))
    while cursor.nextset():
        pass
    row = dict(zip([d.name for d in cursor.description], cursor.fetchone()))
    row.pop("execution_utc")
    return row


def confirmed(name: str, connection) -> str:
    start = connection.execute("SELECT to_char(pg_postmaster_start_time() AT TIME ZONE 'UTC', "
                               "'YYYY-MM-DD HH24:MI:SS.US')").fetchone()[0]
    text = script(name)
    assert text.count(PLACEHOLDER) == 1
    return text.replace(PLACEHOLDER, f"'{start}'")


def test_sql_editor_request_is_one_implicit_transaction(connect):
    """The guarantee the scripts rely on: statements sent together are cancelled together by an error."""
    with connect() as c:
        run_error(c, "CREATE TABLE public.geonaf_probe (id int); SELECT 1 / 0;")
        assert c.execute("SELECT to_regclass('public.geonaf_probe')").fetchone()[0] is None


def test_simulation_succeeds_and_leaves_the_fingerprint_unchanged(connect):
    with connect() as c:
        before = fingerprint(c)
        message, detail = run_error(c, script("09_rename_legacy_schema_simulation.sql"))
        assert message == SIMULATION_OK, message
        assert detail.startswith("declencheurs d'evenement executes : ")
        after = fingerprint(c)
    assert after == before
    assert before["oid_geonaf_legacy_draft"] == "absent" and before["commentaire_geonaf_json"] == "null"
    assert before["tables"] == 8 and before["lecture_seule"] == "on"


@pytest.mark.parametrize("change, expected", [
    ("INSERT INTO geonaf.sources VALUES (1, 'x')", "GEONAF: renommage refuse"),
    ("COMMENT ON SCHEMA geonaf IS 'quelque chose'", "GEONAF: schema geonaf marque, ce n'est pas le schema herite"),
    ("CREATE TABLE geonaf.extra (id int)", "GEONAF: tables inattendues dans geonaf"),
    ("CREATE SCHEMA geonaf_legacy_draft", "GEONAF: le schema geonaf_legacy_draft existe deja"),
])
def test_simulation_guard_refuses_a_schema_that_is_not_the_legacy_one(connect, change, expected):
    with connect() as c:
        c.execute(change)
        before = fingerprint(c)
        assert run_error(c, script("09_rename_legacy_schema_simulation.sql"))[0] == expected
        assert fingerprint(c) == before


@pytest.mark.parametrize("name", ["09_rename_legacy_schema_simulation.sql", "09_rename_legacy_schema_application.sql"])
def test_guard_refuses_a_function_whose_search_path_names_geonaf(connect, name):
    """D2.9 audit: SET search_path = geonaf is kept in pg_proc.proconfig, not in prosrc. Before the fix the
    simulation succeeded; after the rename and 0001, such a function would bind to the NEW geonaf tables."""
    with connect() as c:
        c.execute("CREATE FUNCTION public.geonaf_probe_fn() RETURNS int LANGUAGE sql SET search_path = geonaf "
                  "AS 'SELECT 1'")
        before = fingerprint(c)
        sql = script(name) if "simulation" in name else confirmed(name, c)
        message, detail = run_error(c, sql)
        assert message == "GEONAF: renommage refuse" and "fonction hors geonaf qui cite geonaf" in detail
        assert fingerprint(c) == before


def test_fingerprint_sees_column_grants_and_default_privileges(connect):
    """D2.9 audit: the fingerprint must change with every grant kept for the schema, column grants and
    default privileges included (they are kept by OID through a rename, but must be proved intact)."""
    with connect() as c:
        if not c.execute("SELECT 1 FROM pg_roles WHERE rolname = 'geonaf_fp_probe'").fetchone():
            c.execute("CREATE ROLE geonaf_fp_probe NOLOGIN")
        try:
            base = fingerprint(c)["empreinte_sha256"]
            c.execute("GRANT SELECT (label) ON geonaf.sources TO geonaf_fp_probe")
            column_grant = fingerprint(c)["empreinte_sha256"]
            c.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA geonaf GRANT SELECT ON TABLES TO geonaf_fp_probe")
            default_acl = fingerprint(c)["empreinte_sha256"]
            assert len({base, column_grant, default_acl}) == 3
        finally:
            c.execute("DROP OWNED BY geonaf_fp_probe")     # Its grants and default privileges, this database.
            c.execute("DROP ROLE geonaf_fp_probe")


def _line_ranges(text: str):
    lines = text.splitlines()
    for i in range(len(lines)):
        for j in range(i + 1, len(lines) + 1):
            yield "\n".join(lines[i:j])
    for line in lines:                       # A selection that starts after the "--" of a comment.
        if "--" in line:
            yield line.split("--", 1)[1]


def _harmful_fragments(factory, names) -> list:
    """Fragments that run without error and change the state; each one is cancelled after the check."""
    harmful = []
    with factory(autocommit=False) as c:
        base = c.execute(STATE).fetchone()[0]
        c.rollback()
        for name in names:
            for fragment in _line_ranges(script(name)):
                if not fragment.strip():
                    continue
                try:
                    run(c, fragment)
                    state = c.execute(STATE).fetchone()[0]
                    if state != base:
                        harmful.append((name, fragment[:120], state[:200]))
                except Exception:    # noqa: BLE001 - an error is the expected, harmless outcome.
                    pass
                c.rollback()
    return harmful


NAMES = ("09_rename_legacy_schema_simulation.sql", "09_rename_legacy_schema_application.sql",
         "09_rename_legacy_schema_rollback.sql")


def test_no_line_range_of_the_09_scripts_changes_the_initial_state(connect):
    assert _harmful_fragments(connect, NAMES) == []


def test_no_line_range_of_the_09_scripts_changes_the_renamed_state(connect):
    with connect() as c:
        run(c, confirmed("09_rename_legacy_schema_application.sql", c))
    assert _harmful_fragments(connect, NAMES) == []


def test_application_refuses_to_run_without_the_confirmation(connect):
    with connect() as c:
        before = fingerprint(c)
        message = run_error(c, script("09_rename_legacy_schema_application.sql"))[0]
        assert message == "GEONAF: APPLICATION REFUSEE, confirmation absente ou differente de cette instance"
        wrong = script("09_rename_legacy_schema_application.sql").replace(PLACEHOLDER, "'2026-11-02 16:49:52.000000'")
        assert run_error(c, wrong)[0] == message
        assert fingerprint(c) == before


def test_application_then_rollback_restores_name_oid_and_initial_comment(connect):
    with connect() as c:
        before = fingerprint(c)
        rows = run(c, confirmed("09_rename_legacy_schema_application.sql", c))
        assert rows == [(True, 8, MARK_PREFIX + "null")]
        renamed = fingerprint(c)
        assert renamed["oid_geonaf_legacy_draft"] == before["oid_geonaf"] and renamed["oid_geonaf"] == "absent"
        assert run_error(c, script("09_rename_legacy_schema_rollback.sql"))[0].startswith("GEONAF: RETOUR ARRIERE REFUSE")
        assert fingerprint(c) == renamed
        assert run(c, confirmed("09_rename_legacy_schema_rollback.sql", c)) == [(True, True, 8, "(aucun)")]
        assert fingerprint(c) == before


def _mark_renamed_schema(c, initial_json: str):
    """Rename by hand and write a mark carrying ``initial_json``, as if 09 had found that initial comment."""
    c.execute("ALTER SCHEMA geonaf RENAME TO geonaf_legacy_draft")
    c.execute("SELECT set_config('geonaf.mark', %s, false)", (MARK_PREFIX + initial_json,))
    c.execute("DO $$ BEGIN EXECUTE format('COMMENT ON SCHEMA geonaf_legacy_draft IS %L', "
              "current_setting('geonaf.mark')); END $$")


@pytest.mark.parametrize("initial", ["commentaire d'origine, avec « guillemets » et ; point-virgule",
                                     "ligne 1\nligne 2 \\ barre oblique"])
def test_rollback_restores_a_non_null_initial_comment_exactly(connect, initial):
    """The rollback never assumes NULL: it restores the exact text kept in the mark as a JSON string."""
    import json

    with connect() as c:
        _mark_renamed_schema(c, json.dumps(initial, ensure_ascii=False))
        run(c, confirmed("09_rename_legacy_schema_rollback.sql", c))
        restored = c.execute("SELECT obj_description(to_regnamespace('geonaf'), 'pg_namespace')").fetchone()[0]
    assert restored == initial


@pytest.mark.parametrize("initial_json", ['""', "42", '{"a": 1}', "pas du json"])
def test_rollback_refuses_a_mark_whose_initial_comment_cannot_be_genuine(connect, initial_json):
    """PostgreSQL never stores an empty comment: "" (like a number, an object or invalid JSON) is refused."""
    with connect() as c:
        _mark_renamed_schema(c, initial_json)
        before = c.execute(STATE).fetchone()[0]
        message = run_error(c, confirmed("09_rename_legacy_schema_rollback.sql", c))[0]
        assert message.startswith("GEONAF: commentaire initial illisible") or "json" in message.lower(), message
        assert c.execute(STATE).fetchone()[0] == before


@pytest.mark.parametrize("setup, expected", [
    ("COMMENT ON SCHEMA geonaf_legacy_draft IS 'autre chose'", "GEONAF: marque inattendue sur geonaf_legacy_draft"),
    ("CREATE SCHEMA geonaf", "GEONAF: un schema geonaf existe"),
    ("CREATE TABLE geonaf_legacy_draft.extra (id int)", "GEONAF: tables inattendues dans geonaf_legacy_draft"),
])
def test_rollback_refuses_a_state_it_did_not_create(connect, setup, expected):
    with connect() as c:
        run(c, confirmed("09_rename_legacy_schema_application.sql", c))
        c.execute(setup)
        before = c.execute(STATE).fetchone()[0]
        assert run_error(c, confirmed("09_rename_legacy_schema_rollback.sql", c))[0].startswith(expected)
        assert c.execute(STATE).fetchone()[0] == before


def test_event_trigger_effects_notify_is_cancelled_a_sequence_increment_is_not(connect):
    """Residual risk of the simulation, shown with a trigger shaped like pg_graphql's (needs a superuser)."""
    with connect() as c:
        if not c.execute("SELECT rolsuper FROM pg_roles WHERE rolname = current_user").fetchone()[0]:
            pytest.skip("Un superutilisateur est nécessaire pour créer un déclencheur d'événement.")
        c.execute("CREATE SCHEMA IF NOT EXISTS ddl_probe")
        c.execute("CREATE SEQUENCE IF NOT EXISTS ddl_probe.ddl_counter")
        c.execute("""CREATE OR REPLACE FUNCTION ddl_probe.on_ddl() RETURNS event_trigger LANGUAGE plpgsql AS
                     $f$ BEGIN PERFORM pg_catalog.nextval('ddl_probe.ddl_counter');
                     PERFORM pg_catalog.pg_notify('ddl_probe', tg_tag); END $f$""")
        c.execute("CREATE EVENT TRIGGER ddl_probe_trigger ON ddl_command_end EXECUTE FUNCTION ddl_probe.on_ddl()")
        try:
            with connect() as listener:
                listener.execute("LISTEN ddl_probe")
                start = c.execute("SELECT last_value, is_called FROM ddl_probe.ddl_counter").fetchone()
                message, detail = run_error(c, script("09_rename_legacy_schema_simulation.sql"))
                assert message == SIMULATION_OK and "ddl_probe_trigger:ddl_probe.on_ddl(*)" in detail
                end = c.execute("SELECT last_value, is_called FROM ddl_probe.ddl_counter").fetchone()
                assert end != start                                  # nextval survives the cancellation.
                listener.execute("SELECT 1")
                assert list(listener.notifies(timeout=0.5, stop_after=1)) == []   # NOTIFY was cancelled.
        finally:
            c.execute("DROP EVENT TRIGGER IF EXISTS ddl_probe_trigger")
            c.execute("DROP SCHEMA ddl_probe CASCADE")


@pytest.mark.parametrize("event", ["ddl_command_start", "ddl_command_end"])
def test_a_foreign_gn009_signal_never_reads_as_a_successful_simulation(connect, event):
    """Review D2.9: before the rename_tested flag, a GN009 raised by an event trigger before the rename and its
    checks was caught as the internal signal, and the simulation reported success without having renamed."""
    with connect() as c:
        if not c.execute("SELECT rolsuper FROM pg_roles WHERE rolname = current_user").fetchone()[0]:
            pytest.skip("Un superutilisateur est nécessaire pour créer un déclencheur d'événement.")
        c.execute("CREATE SCHEMA IF NOT EXISTS ddl_probe")
        c.execute("""CREATE OR REPLACE FUNCTION ddl_probe.trap() RETURNS event_trigger LANGUAGE plpgsql AS
                     $f$ BEGIN IF tg_tag = 'ALTER SCHEMA' THEN
                     RAISE EXCEPTION USING ERRCODE = 'GN009', MESSAGE = 'signal etranger'; END IF; END $f$""")
        c.execute(f"CREATE EVENT TRIGGER ddl_probe_trap ON {event} EXECUTE FUNCTION ddl_probe.trap()")
        try:
            before = fingerprint(c)
            message = run_error(c, script("09_rename_legacy_schema_simulation.sql"))[0]
            assert message == "GEONAF: signal GN009 inattendu, simulation non concluante, ne pas appliquer 09"
            assert fingerprint(c) == before
        finally:
            c.execute("DROP EVENT TRIGGER IF EXISTS ddl_probe_trap")
            c.execute("DROP SCHEMA ddl_probe CASCADE")
