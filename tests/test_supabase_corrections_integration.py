"""D2-D4 audit corrections on a LOCAL throwaway PostgreSQL (marker ``postgis``); never Supabase, PostGIS not needed.

Needs ``GEONAF_TEST_DATABASE_URL`` on a local database whose name ends with ``_test``. Only synthetic schemas
and roles are created there, and every test removes them.

Proved on a real server:
* P0.1: the generated preconditions of 12_migration_0002 refuse a missing schema, an unmarked schema (the
  NULL that the former NOT IN let through), an unexpected mark, a missing or different 0001 and an already
  recorded 0002, and accept a marked S4 schema; the whole simulation and application scripts stop on them.
* P0.2: each import checks the environment mark in its transaction before anything else: unmarked or other
  environment refused (EnvironmentIdentityError), right mark accepted (the run then stops on the missing
  PostGIS, proving the check passed and nothing was written before it).
* P0.3: 10 lines 50 to 52 and the 11 rows run on a real catalog and fail closed without PostGIS or roles;
  01b refuses to run without PostGIS.
Not proved here (needs PostGIS): the positive privilege path (USAGE true), 01b granting, the 0002 body.
Destructive fixtures additionally require GEONAF_ALLOW_DESTRUCTIVE_TESTS=1, an exact
GEONAF_TEST_DATABASE_NAME and GEONAF_TEST_INSTANCE_ID matching the database comment.
See tests/postgis_support.py; a _test suffix alone never authorizes access.
"""

import os
from pathlib import Path
import re

import pytest

from src.data import postgis_observations as pgo
from src.data import postgis_territory_registry as pg
from src.data import supabase_sql as s
from src.data.database import EnvironmentIdentityError
from src.data.postgis_session import PostgisUnavailableError

from tests.postgis_support import isolated_connection

ROOT = Path(__file__).resolve().parents[1]
SUPABASE = ROOT / "db" / "supabase"
TEST_URL = os.environ.get("GEONAF_TEST_DATABASE_URL", "").strip()
pytestmark = [pytest.mark.postgis,
              pytest.mark.skipif(not TEST_URL, reason="GEONAF_TEST_DATABASE_URL non défini : base locale requise.")]
BODY_0001 = s.MIGRATION_0001.read_text(encoding="utf-8")


@pytest.fixture
def db():
    pytest.importorskip("psycopg")
    with isolated_connection(TEST_URL, autocommit=True) as c:
        c.execute("DROP SCHEMA IF EXISTS geonaf CASCADE")
        yield c
        c.execute("DROP SCHEMA IF EXISTS geonaf CASCADE")


def preconditions(apply: bool = True) -> str:
    text = s.migration_0002_wrapper(apply=apply)
    start = text.index("DO $$\nDECLARE\n  mark text")
    return text[start:text.index("END $$;", start) + len("END $$;")]


def stub(c, mark=None, migrations=()):
    """geonaf schema with an optional mark and a schema_migrations table holding (version, sha) rows."""
    from psycopg import sql

    c.execute("CREATE SCHEMA geonaf")
    if mark is not None:                          # COMMENT takes no bind parameter: quoted literal.
        c.execute(sql.SQL("COMMENT ON SCHEMA geonaf IS {}").format(sql.Literal(mark)))
    if migrations is not None:
        c.execute("CREATE TABLE geonaf.schema_migrations (version text PRIMARY KEY, body_sha256 char(64))")
        for version, sha in migrations:
            c.execute("INSERT INTO geonaf.schema_migrations VALUES (%s, %s)", (version, sha))


def error_of(c, sql: str) -> str | None:
    import psycopg

    try:
        c.execute(sql)
    except psycopg.Error as error:
        return error.diag.message_primary
    return None


OK_0001 = ("0001_territorial_registry", __import__("hashlib").sha256(BODY_0001.encode("utf-8")).hexdigest())


@pytest.mark.parametrize("setup, expected", [
    (None, "GEONAF: schema geonaf absent, 0002 refusee"),
    (dict(mark=None, migrations=[OK_0001]), "GEONAF: schema geonaf non marque (03 a executer avant 0002)"),
    (dict(mark="geonaf-legacy: schema herite", migrations=[OK_0001]),
     "GEONAF: marque inattendue sur le schema geonaf, 0002 refusee"),
    (dict(mark="geonaf-environment:prod", migrations=[OK_0001]),
     "GEONAF: marque inattendue sur le schema geonaf, 0002 refusee"),
    (dict(mark="geonaf-environment:test", migrations=None),
     "GEONAF: migration 0001 non enregistree (geonaf.schema_migrations absente), 0002 refusee"),
    (dict(mark="geonaf-environment:test", migrations=[]),
     "GEONAF: migration 0001 absente ou differente du corps revu, 0002 refusee"),
    (dict(mark="geonaf-environment:test", migrations=[("0001_territorial_registry", "0" * 64)]),
     "GEONAF: migration 0001 absente ou differente du corps revu, 0002 refusee"),
    (dict(mark="geonaf-environment:dev", migrations=[OK_0001, ("0002_observations", "1" * 64)]),
     "GEONAF: migration 0002 deja enregistree"),
    (dict(mark="geonaf-environment:dev", migrations=[OK_0001]), None),            # S4: accepted.
    (dict(mark="geonaf-environment:test", migrations=[OK_0001]), None),
])
@pytest.mark.parametrize("apply", [True, False])
def test_migration_0002_preconditions_on_a_real_server(db, setup, expected, apply):
    if setup is not None:
        stub(db, **setup)
    assert error_of(db, preconditions(apply)) == expected


@pytest.mark.parametrize("name", ["12_migration_0002_simulation.sql", "12_migration_0002_application.sql"])
def test_whole_script_12_stops_on_an_unmarked_schema_and_writes_nothing(db, name):
    stub(db, mark=None, migrations=[OK_0001])
    tables = "SELECT count(*) FROM pg_class WHERE relnamespace = 'geonaf'::regnamespace"
    before = db.execute(tables).fetchone()[0]
    message = error_of(db, (SUPABASE / name).read_text(encoding="utf-8"))
    assert message == "GEONAF: schema geonaf non marque (03 a executer avant 0002)"
    assert db.execute(tables).fetchone()[0] == before


# --- P0.2: imports check the environment in their transaction ----------------------------------------------


IMPORTS = {
    "territories": lambda c, env: pg.apply_import(c, pg.ImportPlan((), {}), source_label="lot", code_version="t",
                                                  expected_environment=env, apply=True),
    "acquisitions": lambda c, env: pgo.apply_acquisition_import(c, [], source_label="lot", code_version="t",
                                                                expected_environment=env, apply=True),
    "indicators": lambda c, env: pgo.apply_indicator_import(c, [], source_label="lot", code_version="t",
                                                            expected_environment=env, apply=True),
}


@pytest.mark.parametrize("name", sorted(IMPORTS))
@pytest.mark.parametrize("mark, error", [
    (None, EnvironmentIdentityError),
    ("geonaf-environment:dev", EnvironmentIdentityError),
    ("geonaf-environment:test", PostgisUnavailableError),     # Identity accepted; stops at R5 bis, no PostGIS.
])
def test_imports_verify_the_mark_on_a_real_transaction_before_anything_else(db, name, mark, error):
    import psycopg

    if db.execute("SELECT 1 FROM pg_extension WHERE extname = 'postgis'").fetchone():
        pytest.skip("PostGIS présent : ce test prouve l'ordre par l'absence de PostGIS.")
    stub(db, mark=mark, migrations=None)
    with isolated_connection(TEST_URL) as connection:
        with pytest.raises(error):
            IMPORTS[name](connection, "test")
        assert connection.info.transaction_status == psycopg.pq.TransactionStatus.IDLE      # Rolled back.
    assert db.execute("SELECT count(*) FROM pg_class WHERE relnamespace = 'geonaf'::regnamespace").fetchone()[0] == 0


def test_connect_target_reaches_a_local_server_under_the_tls_policy(db):
    from src.data.database import parse_target
    from src.data.postgis_session import connect_target

    target = parse_target(TEST_URL.replace("postgresql://", "postgresql+psycopg://", 1), "test")
    assert target.sslmode is None                                   # Loopback: TLS optional by policy.
    with connect_target(target) as connection:
        assert connection.execute("SELECT 1").fetchone()[0] == 1


# --- P0.3: PostGIS privilege checks fail closed on a real catalog ------------------------------------------


def run(c, sql):
    cursor = c.cursor()
    cursor.execute(sql.encode("utf-8"))
    while cursor.nextset():
        pass
    return cursor.fetchall()


@pytest.fixture
def geonaf_roles(db):
    created = []
    for role in ("geonaf_reader", "geonaf_importer"):
        if not db.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone():
            db.execute(f"CREATE ROLE {role} NOLOGIN")
            created.append(role)
    yield
    for role in created:
        db.execute(f"DROP ROLE {role}")


def test_security_precheck_postgis_lines_fail_closed_without_postgis(db, geonaf_roles):
    rows = {r[0]: r for r in run(db, (SUPABASE / "10_security_precheck.sql").read_text(encoding="utf-8"))}
    usage = rows["USAGE sur le schema de PostGIS pour geonaf_reader et geonaf_importer"]
    execute = next(r for k, r in rows.items() if k.startswith("EXECUTE de st_intersects"))
    create = rows["roles GEONAF autorises a creer dans le schema de PostGIS (moindre privilege)"]
    assert (usage[2], usage[3]) == ("false", False) and (execute[2], execute[3]) == ("false", False)
    assert (create[2], create[3]) == ("aucun", True)


def test_state_diagnostic_reports_the_postgis_privileges_of_each_role(db, geonaf_roles):
    rows = {(r[1], r[2]): r for r in run(db, (SUPABASE / "11_state_diagnostic.sql").read_text(encoding="utf-8"))}
    for role in ("geonaf_reader", "geonaf_importer"):
        row = rows[("droits", f"{role} : PostGIS (usage, execute ST_*, create)")]
        assert row[3] == "PostGIS absent" and row[4] is True


def test_state_diagnostic_never_calls_a_privilege_function_for_an_absent_role(db):
    rows = {(r[1], r[2]): r for r in run(db, (SUPABASE / "11_state_diagnostic.sql").read_text(encoding="utf-8"))}
    assert rows[("droits", "geonaf_reader : PostGIS (usage, execute ST_*, create)")][3] == "role absent"


def test_grant_script_refuses_to_run_without_postgis_and_grants_nothing(db, geonaf_roles):
    before = db.execute("SELECT count(*) FROM pg_namespace WHERE has_schema_privilege('geonaf_reader', oid, 'USAGE')"
                        ).fetchone()[0]
    assert error_of(db, (SUPABASE / "01b_grant_postgis_usage.sql").read_text(encoding="utf-8")) == \
        "GEONAF: extension PostGIS absente, rien a accorder"
    after = db.execute("SELECT count(*) FROM pg_namespace WHERE has_schema_privilege('geonaf_reader', oid, 'USAGE')"
                       ).fetchone()[0]
    assert after == before


def test_grant_script_holds_no_runnable_grant_outside_its_template():
    """Partial selection (D2.9): the GRANT exists only as a format() template; never ALL or CREATE."""
    text = (SUPABASE / "01b_grant_postgis_usage.sql").read_text(encoding="utf-8")
    code = "\n".join(line.split("--")[0] for line in text.splitlines())
    assert re.findall(r"GRANT\s+\w+", code) == ["GRANT USAGE"]
    assert "format('GRANT USAGE ON SCHEMA %I TO %I'" in code
    assert not re.search(r"GRANT\s+(ALL|CREATE)", text, re.I)
