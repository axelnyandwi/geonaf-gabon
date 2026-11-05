"""PostgreSQL connection layer (D2.1) without any database or network access.

Configuration, environment guard and diagnostic logic are pure. Tests marked as needing SQLAlchemy
and psycopg (requirements-db.txt) are skipped when those optional packages are absent; the only
socket they open is a loopback port where nothing listens.
"""

from pathlib import Path
import re
import subprocess
import sys

import pytest

from src.data import database as db
from src.data import database_diagnostic as diag

ROOT = Path(__file__).resolve().parents[1]
PASSWORD = "s3cr%40t-pw"           # URL-encoded "s3cr@t-pw"
REMOTE = f"postgresql+psycopg://postgres.abcdref:{PASSWORD}@aws-0-eu-west-3.pooler.supabase.com:6543/postgres"


def assert_no_secret(text):
    assert PASSWORD not in text and "s3cr@t-pw" not in text and REMOTE not in text


@pytest.fixture(scope="module")
def ca_file(tmp_path_factory):
    """Synthetic PEM CA; no remote TLS handshake is performed."""
    trustme = pytest.importorskip("trustme")
    path = tmp_path_factory.mktemp("ca") / "geonaf-test-ca.pem"
    trustme.CA().cert_pem.write_to_path(path)
    return str(path)


@pytest.fixture
def remote(ca_file):
    """Remote target as the policy requires it: verify-full (default) and an explicit CA file."""
    return lambda environment="dev", url=REMOTE: db.parse_target(url, environment, sslrootcert=ca_file)


# --- Backend selection ---------------------------------------------------------------------------


def test_backend_defaults_to_file():
    assert db.selected_backend({}) == "file"
    assert db.selected_backend({db.BACKEND_VARIABLE: "  "}) == "file"


def test_backend_accepts_postgres_and_refuses_anything_else():
    assert db.selected_backend({db.BACKEND_VARIABLE: "Postgres"}) == "postgres"
    with pytest.raises(db.DatabaseConfigError, match=db.BACKEND_VARIABLE):
        db.selected_backend({db.BACKEND_VARIABLE: "supabase"})


def test_selected_backend_reads_the_real_environment_by_default(monkeypatch):
    monkeypatch.delenv(db.BACKEND_VARIABLE, raising=False)
    assert db.selected_backend() == "file"


# --- Target parsing ------------------------------------------------------------------------------


def test_remote_target_is_parsed_and_server_verification_is_required_by_default(remote, ca_file):
    target = remote("dev")
    assert (target.host, target.port, target.database, target.user) == (
        "aws-0-eu-west-3.pooler.supabase.com", 6543, "postgres", "postgres.abcdref")
    assert (target.sslmode, target.sslrootcert) == ("verify-full", ca_file)
    assert target.password == "s3cr@t-pw"
    assert db.tls_options(target) == {"sslmode": "verify-full", "sslrootcert": ca_file}


@pytest.mark.parametrize("mode", ["require", "verify-ca", "disable", "allow", "prefer"])
def test_remote_target_refuses_any_mode_weaker_than_verify_full(remote, mode):
    """require encrypts without authenticating the server; verify-ca skips the host name check."""
    with pytest.raises(db.DatabaseConfigError, match="verify-full") as raised:
        remote("dev", REMOTE + f"?sslmode={mode}")
    assert_no_secret(str(raised.value))


def test_remote_target_refuses_a_missing_or_unreadable_ca_file_without_naming_the_path(tmp_path):
    with pytest.raises(db.DatabaseConfigError, match=db.SSLROOTCERT_VARIABLE):
        db.parse_target(REMOTE, "dev")
    absent = tmp_path / "absent-ca.pem"
    with pytest.raises(db.DatabaseConfigError) as raised:
        db.parse_target(REMOTE, "dev", sslrootcert=str(absent))
    assert str(absent) not in str(raised.value) and "absent-ca" not in str(raised.value)
    with pytest.raises(db.DatabaseConfigError):                       # libpq 14: "system" is not a keyword.
        db.parse_target(REMOTE, "dev", sslrootcert="system")


def test_ca_file_comes_from_the_environment_and_is_kept_out_of_repr(ca_file):
    target = db.load_target({db.URL_VARIABLE: REMOTE, db.ENV_VARIABLE: "test", db.SSLROOTCERT_VARIABLE: ca_file})
    assert target.sslrootcert == ca_file and ca_file not in repr(target) and ca_file not in target.describe()


def test_plain_postgresql_scheme_is_normalised_to_psycopg3(remote):
    target = remote("test", REMOTE.replace("postgresql+psycopg://", "postgresql://"))
    assert target.url.startswith("postgresql+psycopg://")


@pytest.mark.parametrize("environment", ["", "prod", "production", "staging", None])
def test_only_dev_and_test_environments_are_authorised(environment):
    with pytest.raises(db.DatabaseConfigError, match=db.ENV_VARIABLE):
        db.parse_target(REMOTE, environment)


@pytest.mark.parametrize("url", [
    REMOTE.replace("postgresql+psycopg", "postgresql+psycopg2"),
    REMOTE.replace("postgresql+psycopg", "postgresql+asyncpg"),
    REMOTE.replace(":6543", ":port"),
    REMOTE.replace("/postgres", "/"),
    REMOTE + "?sslmode=disable",
    REMOTE + "?sslmode=prefer",
])
def test_invalid_urls_are_refused_without_echoing_them(url):
    with pytest.raises(db.DatabaseConfigError) as raised:
        db.parse_target(url, "dev")
    assert_no_secret(str(raised.value))


def test_missing_url_names_the_variable_only():
    with pytest.raises(db.DatabaseConfigError, match=db.URL_VARIABLE):
        db.load_target({db.ENV_VARIABLE: "dev"})


def test_local_database_may_run_without_tls_and_explicit_sslmode_is_kept(remote):
    """Policy: TLS optional on loopback only (throwaway test servers); never a weaker remote rule."""
    local = db.parse_target("postgresql+psycopg://geonaf@127.0.0.1:5433/geonaf_test", "test")
    assert local.sslmode is None and local.sslrootcert is None and db.tls_options(local) == {}
    kept = db.parse_target("postgresql+psycopg://geonaf@localhost:5433/geonaf_test?sslmode=require", "test")
    assert kept.sslmode == "require"
    verified = remote("dev", REMOTE + "?sslmode=verify-full")
    assert verified.sslmode == "verify-full"


def test_password_never_appears_in_repr_or_description(remote):
    target = remote("dev")
    assert_no_secret(repr(target))
    assert_no_secret(target.describe())
    for value in (target.host, target.user, target.database, target.sslrootcert):
        assert value not in target.describe() and value not in repr(target)


def test_redact_removes_url_and_password(remote):
    target = remote("dev")
    message = f"failed for {target.url} with password s3cr@t-pw"
    assert_no_secret(db.redact(message, target))


# --- Environment guard ---------------------------------------------------------------------------


def test_identity_accepts_only_the_declared_environment_mark():
    db.check_identity("geonaf-environment:dev", "dev")
    with pytest.raises(db.EnvironmentIdentityError, match="DB_ENV_MISMATCH"):
        db.check_identity("geonaf-environment:test", "dev")
    with pytest.raises(db.EnvironmentIdentityError, match="aucune marque"):
        db.check_identity(None, "test")


def test_expected_tables_match_the_supabase_migration():
    migration = ROOT / "db" / "supabase" / "02_migration_0001_application.sql"
    if not migration.exists():
        pytest.skip("Migration Supabase absente de cette copie du dépôt.")
    sql = migration.read_text(encoding="utf-8")
    created = set(re.findall(r"CREATE TABLE geonaf\.(\w+)", sql))
    assert created == set(db.EXPECTED_TABLES)


# --- Diagnostic logic (fake database) ------------------------------------------------------------


def fake_fetch(mark="geonaf-environment:dev", schema=True, tables=db.EXPECTED_TABLES, postgis=True):
    answers = {
        diag.CONNECTION_SQL: [("postgres", "17.11")],
        diag.SCHEMA_SQL: [(schema,)],
        diag.MARK_SQL: [(mark,)],
        diag.TABLES_SQL: [(name,) for name in tables],
        diag.POSTGIS_SQL: [("3.3.7", "extensions")] if postgis else [],
        'SELECT "extensions".postgis_lib_version()': [("3.3.7",)],
    }
    calls = []

    def fetch(sql, params):
        calls.append(sql)
        return answers[sql]
    fetch.calls = calls
    return fetch


def statuses(checks):
    return [check.status for check in checks]


def test_diagnostic_passes_on_a_conforming_database():
    checks = diag.run_checks(fake_fetch(), "dev")
    assert statuses(checks) == [diag.OK] * 5
    assert "PostgreSQL 17.11" in checks[0].detail
    assert checks[4].detail == "PostGIS 3.3.7 (bibliothèque 3.3.7, schéma « extensions »)"


def test_wrong_environment_stops_before_reading_the_schema():
    fetch = fake_fetch(mark="geonaf-environment:test")
    checks = diag.run_checks(fetch, "dev")
    assert statuses(checks) == [diag.OK, diag.OK, diag.FAILED, diag.SKIPPED, diag.SKIPPED]
    assert diag.TABLES_SQL not in fetch.calls and diag.POSTGIS_SQL not in fetch.calls


def test_unmarked_database_is_refused():
    checks = diag.run_checks(fake_fetch(mark=None), "dev")
    assert checks[2].status == diag.FAILED and "aucune marque" in checks[2].detail


def test_missing_schema_is_reported():
    checks = diag.run_checks(fake_fetch(schema=False), "dev")
    assert statuses(checks)[1:] == [diag.FAILED, diag.SKIPPED, diag.SKIPPED, diag.SKIPPED]


def test_missing_and_unexpected_tables_are_named():
    tables = [t for t in db.EXPECTED_TABLES if t != "geometries"] + ["scratch"]
    checks = diag.run_checks(fake_fetch(tables=tables), "dev")
    assert checks[3].status == diag.FAILED
    assert "tables absentes : geometries" in checks[3].detail and "scratch" in checks[3].detail


def test_unexpected_extra_table_alone_is_reported_without_failing():
    checks = diag.run_checks(fake_fetch(tables=[*db.EXPECTED_TABLES, "scratch"]), "dev")
    assert checks[3].status == diag.OK and "non attendues : scratch" in checks[3].detail


def test_missing_postgis_is_reported():
    checks = diag.run_checks(fake_fetch(postgis=False), "dev")
    assert checks[4].status == diag.FAILED and "postgis" in checks[4].detail


def test_postgis_schema_name_is_quoted():
    assert diag.quote_identifier('odd"name') == '"odd""name"'


def test_report_is_ok_only_when_every_check_passed():
    passed = diag.DiagnosticReport("x", (diag.Check("a", diag.OK, ""),))
    skipped = diag.DiagnosticReport("x", (diag.Check("a", diag.OK, ""), diag.Check("b", diag.SKIPPED, "")))
    assert passed.ok and not skipped.ok


# --- Import safety -------------------------------------------------------------------------------


def test_importing_the_modules_loads_no_database_driver():
    code = ("import sys; import src.data.database, src.data.database_diagnostic; "
            "print(sorted(m for m in ('sqlalchemy', 'psycopg', 'psycopg2') if m in sys.modules))")
    result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, check=True)
    assert result.stdout.strip() == "[]"


def test_application_code_reaches_the_database_only_through_the_storage_layer():
    """D4: the interface opens the store of src.data.storage; it never handles sessions, drivers or SQL.

    errors.py may import the database error *types* (lazily) to show their user messages.
    """
    sources = [ROOT / "app.py", *(ROOT / "views").rglob("*.py"), *(ROOT / "src" / "components").rglob("*.py")]
    forbidden = re.compile(r"(psycopg|sqlalchemy|src\.data\.postgis_\w+|postgis_session)")
    assert [p.name for p in sources if forbidden.search(p.read_text(encoding="utf-8"))
            and p.name != "errors.py"] == []
    users = sorted(p.name for p in sources if "src.data.database" in p.read_text(encoding="utf-8"))
    assert users == ["errors.py"]
    errors = (ROOT / "src" / "components" / "errors.py").read_text(encoding="utf-8")
    assert "    from src.data.database import DatabaseConfigError" in errors     # Inside a function (lazy).
    assert sorted(p.name for p in sources if "src.data.storage" in p.read_text(encoding="utf-8")) == [
        "data_source.py"]


# --- Engine and session (need requirements-db.txt; no remote access) -------------------------------


@pytest.fixture
def drivers():
    pytest.importorskip("sqlalchemy", minversion="2.0")
    pytest.importorskip("psycopg", minversion="3.1")


def test_engine_uses_no_client_pool_and_pooler_safe_options(drivers, remote):
    from sqlalchemy.pool import NullPool

    engine = db.create_engine(remote("dev"))
    try:
        assert isinstance(engine.pool, NullPool)
        assert engine.dialect.driver == "psycopg"
        assert engine.hide_parameters
        assert engine.url.host is None and engine.url.username is None
        assert "pooler.supabase.com" not in repr(engine)
        assert_no_secret(repr(engine))
    finally:
        engine.dispose()


def test_connect_args_carry_timeout_tls_and_no_prepared_statements(drivers, monkeypatch, remote, ca_file):
    import sqlalchemy
    import psycopg

    seen, received = {}, {}
    monkeypatch.setattr(sqlalchemy, "create_engine", lambda url, **kwargs: seen.update(kwargs) or "engine")
    monkeypatch.setattr(psycopg, "connect", lambda **kwargs: received.update(kwargs))
    assert db.create_engine(remote("dev")) == "engine"
    seen["creator"]()
    assert received["connect_timeout"] == db.CONNECT_TIMEOUT_S
    assert received["prepare_threshold"] is None and received["application_name"] == "geonaf"
    assert received["sslmode"] == "verify-full" and received["sslrootcert"] == ca_file


def test_unreachable_database_raises_a_redacted_connection_error(drivers):
    # Port 1 on loopback: nothing listens, the refusal is immediate and no remote host is contacted.
    url = f"postgresql+psycopg://geonaf:{PASSWORD}@127.0.0.1:1/geonaf_test"
    target = db.parse_target(url, "test")
    with pytest.raises(db.DatabaseConnectionError) as raised:
        with db.read_only_session(target):
            pytest.fail("Aucune connexion ne doit être remise.")
    assert_no_secret(str(raised.value))
    assert raised.value.__cause__ is None and raised.value.__suppress_context__


def test_diagnostic_reports_an_unreachable_database_as_a_failed_connection(drivers):
    target = db.parse_target(f"postgresql+psycopg://geonaf:{PASSWORD}@127.0.0.1:1/geonaf_test", "test")
    report = diag.diagnose(target)
    assert not report.ok
    assert statuses(report.checks) == [diag.FAILED] + [diag.SKIPPED] * 4
    assert_no_secret(report.checks[0].detail)
    assert_no_secret(report.target)


class FakeResult:
    def __init__(self, value):
        self.value = value

    def scalar(self):
        return self.value


class FakeConnection:
    """Records statements; answers the environment-mark query with ``mark``."""

    def __init__(self, mark):
        self.mark, self.statements, self.closed, self.rolled_back = mark, [], False, False

    def begin(self):
        self.statements.append("BEGIN")

    def execute(self, statement, params=None):
        self.statements.append(str(statement))
        return FakeResult(self.mark)

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


class FakeEngine:
    def __init__(self, connection):
        self.connection = connection

    def connect(self):
        return self.connection


def test_session_is_read_only_rolled_back_and_closed(drivers, remote):
    connection = FakeConnection("geonaf-environment:dev")
    with db.read_only_session(remote("dev"), engine=FakeEngine(connection)) as session:
        assert session is connection
    assert connection.statements[:3] == ["BEGIN", "SET TRANSACTION READ ONLY",
                                         "SELECT set_config('statement_timeout', :timeout, true)"]
    assert connection.rolled_back and connection.closed


def test_session_refuses_a_database_of_another_environment(drivers, remote):
    connection = FakeConnection("geonaf-environment:dev")
    with pytest.raises(db.EnvironmentIdentityError):
        with db.read_only_session(remote("test"), engine=FakeEngine(connection)):
            pytest.fail("La connexion ne doit pas être remise.")
    assert connection.rolled_back and connection.closed


def test_session_closes_the_connection_when_the_caller_fails(drivers, remote):
    connection = FakeConnection("geonaf-environment:dev")
    with pytest.raises(KeyError):
        with db.read_only_session(remote("dev"), engine=FakeEngine(connection)):
            raise KeyError("échec de l'appelant")
    assert connection.rolled_back and connection.closed
