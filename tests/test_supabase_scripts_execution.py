"""Real execution of the Supabase SQL Editor scripts on a LOCAL throwaway PostgreSQL (D2.10a).

Each script is sent whole, as the SQL Editor sends it (one simple-protocol request, one implicit transaction).
Needs ``GEONAF_TEST_DATABASE_URL`` on a local database whose name ends with ``_test`` (marker ``postgis``).
Never Supabase. Skips name the missing capability explicitly, so that a run never claims more than it proved:
- PostgreSQL 17: ``test_the_scripts_were_run_on_postgresql_17`` is skipped below 17 (Supabase runs 17.11);
- PostGIS: 02, 03, 04, 90 and 99 need migration 0001, which needs PostGIS;
- libxml: 08 counts rows with query_to_xml.
Already executed elsewhere: 09 simulation, application, rollback and fingerprint
(test_supabase_legacy_rename_integration.py), 10_verify_legacy_schema_rename.sql
(test_supabase_legacy_verify_integration.py), scripts/db_backup (test_db_backup_scripts.py).
"""

import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SUPABASE = ROOT / "db" / "supabase"
TEST_URL = os.environ.get("GEONAF_TEST_DATABASE_URL", "").strip()
pytestmark = [pytest.mark.postgis,
              pytest.mark.skipif(not TEST_URL, reason="GEONAF_TEST_DATABASE_URL non défini : base locale requise.")]
LEGACY = ("acquisitions", "analysis_acquisitions", "analysis_runs", "indicator_definitions", "indicator_results",
          "sources", "territories", "territory_versions")
GEONAF_ROLES = ("geonaf_importer", "geonaf_reader")


def conninfo() -> str:
    return "postgresql://" + TEST_URL.split("://", 1)[1] if TEST_URL.startswith("postgresql+psycopg://") else TEST_URL


@pytest.fixture
def db():
    psycopg = pytest.importorskip("psycopg")
    from psycopg.conninfo import conninfo_to_dict

    info = conninfo_to_dict(conninfo())
    if not str(info.get("dbname", "")).endswith("_test") or info.get("host") not in ("127.0.0.1", "localhost", "::1"):
        pytest.fail("Base locale dont le nom se termine par « _test » exigée.")
    with psycopg.connect(conninfo(), autocommit=True) as c:
        if c.execute("SELECT count(*) FROM pg_roles WHERE rolname = ANY(%s)", (list(GEONAF_ROLES),)).fetchone()[0]:
            pytest.fail("Rôles GEONAF déjà présents sur le serveur local : nettoyer avant de tester 01.")
        for schema in ("geonaf", "geonaf_legacy_draft"):
            c.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        yield c
        for schema in ("geonaf", "geonaf_legacy_draft"):
            c.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        for role in GEONAF_ROLES:
            c.execute(f"DROP ROLE IF EXISTS {role}")


def legacy_schema(c):
    c.execute("CREATE SCHEMA geonaf")
    for table in LEGACY:
        c.execute(f"CREATE TABLE geonaf.{table} (id bigint PRIMARY KEY, label text)")
        c.execute(f"ALTER TABLE geonaf.{table} ENABLE ROW LEVEL SECURITY")


def run(c, name: str) -> tuple[list[str], list[tuple]]:
    """The whole file in one request; returns the columns and rows of the last result."""
    cursor = c.execute((SUPABASE / name).read_text(encoding="utf-8").encode("utf-8"))
    columns, rows = None, None
    while True:
        if cursor.description:
            columns, rows = [d.name for d in cursor.description], cursor.fetchall()
        if not cursor.nextset():
            return columns, rows


def lines(columns, rows) -> dict:
    """Checks by label: (attendu, obtenu, conforme)."""
    i = {name: columns.index(name) for name in ("verification", "attendu", "obtenu", "conforme")}
    return {r[i["verification"]]: (r[i["attendu"]], r[i["obtenu"]], r[i["conforme"]]) for r in rows}


def server_major(c) -> int:
    return int(c.execute("SELECT current_setting('server_version_num')::int / 10000").fetchone()[0])


def has_extension(c, name: str) -> bool:
    return bool(c.execute("SELECT count(*) FROM pg_available_extensions WHERE name = %s", (name,)).fetchone()[0])


def has_libxml(c) -> bool:
    try:
        c.execute("SELECT xmlparse(content '<a/>')")
        return True
    except Exception:        # noqa: BLE001 - FeatureNotSupported without libxml.
        return False


# --- Environment ---------------------------------------------------------------------------------------------

def test_the_scripts_were_run_on_postgresql_17(db):
    """Marks the run as a PostgreSQL 17 validation only when the test server really is 17 or later."""
    version = db.execute("SELECT current_setting('server_version')").fetchone()[0]
    if server_major(db) < 17:
        pytest.skip(f"Serveur de test PostgreSQL {version} : scripts NON validés sur PostgreSQL 17 (Supabase 17.11).")


# --- 00, 01, 10 ----------------------------------------------------------------------------------------------

def test_preflight_runs_as_one_transaction_and_reports_its_checks(db):
    result = lines(*run(db, "00_preflight.sql"))
    assert result["texte execute en une seule transaction"] == ("meme-transaction", "meme-transaction", True)
    assert result["PostgreSQL 15 ou plus"][1] == "true"
    assert result["schema geonaf"][1] == "absent (migration a appliquer)"
    assert result["roles GEONAF existants"][1] == "aucun"
    assert result["schema geonaf non expose par la Data API"][2] is True
    # PostGIS and the current role depend on the server: reported, never forced here.
    assert set(result) >= {"PostGIS installe", "PostGIS 3.3 ou plus", "role courant"}


def test_roles_script_creates_two_nologin_roles_and_is_idempotent(db):
    for _ in range(2):                                        # Second run: no error, same roles, nothing added.
        columns, rows = run(db, "01_roles.sql")
        assert columns == ["rolname", "rolcanlogin", "rolsuper", "rolbypassrls"]
        assert rows == [("geonaf_importer", False, False, False), ("geonaf_reader", False, False, False)]


def test_security_precheck_before_rename_and_without_roles(db):
    legacy_schema(db)
    result = lines(*run(db, "10_security_precheck.sql"))
    assert result["transaction en lecture seule"] == ("on", "on", True)
    phase = result["phase detectee"][1]
    assert "avant renommage (09 non applique)" in phase and "roles GEONAF absents (01 non applique)" in phase
    for label in ("schema geonaf absent (renomme par 09, migration a appliquer)", "schema herite renomme et marque geonaf-legacy",
                  "roles GEONAF presents"):
        assert result[label][2] is False, label                   # Lines 2, 3, 20: expected false at this phase.


def test_security_precheck_after_rename_and_roles(db):
    legacy_schema(db)
    db.execute("ALTER SCHEMA geonaf RENAME TO geonaf_legacy_draft")
    db.execute("COMMENT ON SCHEMA geonaf_legacy_draft IS 'geonaf-legacy: test ; commentaire initial (JSON) : null'")
    run(db, "01_roles.sql")
    result = lines(*run(db, "10_security_precheck.sql"))
    assert result["phase detectee"][1] == "apres renommage, avant 0001"
    for label in ("schema geonaf absent (renomme par 09, migration a appliquer)", "schema herite renomme et marque geonaf-legacy",
                  "roles GEONAF presents", "roles GEONAF sans LOGIN, SUPERUSER, BYPASSRLS, CREATEROLE, CREATEDB ni REPLICATION",
                  "roles GEONAF membres d'un autre role (droits herites)", "roles GEONAF avec des parametres propres",
                  "droits des roles API et de PUBLIC sur geonaf_legacy_draft", "geonaf ou geonaf_legacy_draft dans pgrst.db_schemas"):
        assert result[label][2] is True, (label, result[label])
    # Line 32, the 42725 case, returns a value; nothing was written by the read-only request.
    assert result["droits par defaut globaux pour les roles API ou PUBLIC (proprietaire:type:beneficiaire)"][1]
    assert db.execute("SELECT count(*) FROM pg_namespace WHERE nspname = 'geonaf'").fetchone()[0] == 0


# --- Scripts that need a capability of the server -------------------------------------------------------------

def test_legacy_inventory_08_runs_on_the_legacy_schema(db):
    if not has_libxml(db):
        pytest.skip("Serveur local sans libxml : 08 (query_to_xml) non exécuté ici ; analyse des types seulement "
                    "(test_sql_char_concatenation.py).")
    legacy_schema(db)
    result = lines(*run(db, "08_inspect_legacy_schema.sql"))
    assert result["transaction en lecture seule"][1] == "on"
    assert result["lignes dans les tables du schema herite"][1] == "0"


@pytest.mark.parametrize("name", ["02_migration_0001_simulation.sql", "04_verify_schema.sql", "90_test_constraints.sql"])
def test_scripts_that_need_migration_0001(db, name):
    if not has_extension(db, "postgis"):
        pytest.skip(f"PostGIS indisponible sur le serveur local : {name} non exécuté (exige la migration 0001).")
    pytest.skip(f"{name} : exécution avec PostGIS non automatisée dans ce lot (schéma extensions de Supabase à reproduire).")
