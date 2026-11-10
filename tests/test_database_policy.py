"""Connection policy at the driver boundary; synthetic targets, no database access."""
from dataclasses import replace
import os
from urllib.parse import quote

import pytest

from src.data import database as db
from src.data.postgis_session import connect_target

LOCAL = "postgresql://synthetic@localhost/synthetic_test"
REMOTE = "postgresql://synthetic:synthetic-password@remote.invalid/synthetic_test"


@pytest.fixture
def ca(tmp_path):
    trustme = pytest.importorskip("trustme")
    path = tmp_path / "synthetic-ca.pem"
    trustme.CA().cert_pem.write_to_path(path)
    return str(path)


@pytest.fixture
def driver(monkeypatch):
    psycopg = pytest.importorskip("psycopg")
    calls = []
    monkeypatch.setattr(psycopg, "connect", lambda *args, **kwargs: calls.append((args, kwargs)) or object())
    return calls


@pytest.mark.parametrize("query", [
    "host=remote.invalid&sslmode=disable", "hostaddr=192.0.2.1&sslmode=disable",
    "host=localhost", "host=localhost&host=remote.invalid", "h%6fst=remote.invalid",
    "service=synthetic", "servicefile=synthetic", "port=6543", "dbname=other",
    "sslmode=verify-full&sslmode=disable", "sslmode=disable&sslmode=verify-full",
    "sslmode=", "sslrootcert=", "sslrootcert=a&sslrootcert=b", "sslmode",
    "options=-csearch_path=public", "gssencmode=prefer", "requiressl=0",
])
def test_ambiguous_or_unapproved_uri_parameters_are_refused(query):
    with pytest.raises(db.DatabaseConfigError):
        db.parse_target(LOCAL + "?" + query, "test")


@pytest.mark.parametrize("authority", [
    "localhost,remote.invalid", "localhost:5432,remote.invalid:5433",
    "localhost%2Cremote.invalid", "%2Ftmp", "%40socket", "[::1],remote.invalid",
])
def test_multiple_destinations_and_unix_sockets_are_refused(authority):
    with pytest.raises(db.DatabaseConfigError):
        db.parse_target("postgresql://synthetic@" + authority + "/synthetic_test", "test")


@pytest.mark.parametrize("url", ["postgresql:///synthetic_test?host=/tmp", LOCAL + "#host=remote.invalid"])
def test_implicit_socket_and_uri_fragment_are_refused(url):
    with pytest.raises(db.DatabaseConfigError):
        db.parse_target(url, "test")


@pytest.mark.parametrize("variable", [
    "PGHOST", "PGHOSTADDR", "PGPORT", "PGDATABASE", "PGUSER", "PGPASSWORD", "PGPASSFILE",
    "PGSERVICE", "PGSERVICEFILE", "PGSYSCONFDIR", "PGSSLMODE", "PGREQUIRESSL", "PGSSLROOTCERT",
    "PGGSSENCMODE", "PGOPTIONS",
])
def test_implicit_libpq_environment_is_refused_at_connection_time(monkeypatch, driver, variable):
    target = db.parse_target(LOCAL, "test")
    monkeypatch.setenv(variable, "sensitive-value-never-display")
    with pytest.raises(db.DatabaseConfigError) as raised:
        connect_target(target)
    assert "sensitive-value" not in str(raised.value)
    assert driver == []


def test_supplied_environment_also_refuses_libpq_overrides():
    with pytest.raises(db.DatabaseConfigError):
        db.load_target({db.URL_VARIABLE: LOCAL, db.ENV_VARIABLE: "test", "PGHOSTADDR": "192.0.2.1"})


@pytest.mark.parametrize("host, address", [("localhost", "127.0.0.1"), ("127.0.0.1", "127.0.0.1"), ("[::1]", "::1")])
def test_local_connection_pins_a_loopback_address_at_driver_boundary(driver, host, address):
    target = db.parse_target(LOCAL.replace("localhost", host), "test")
    connect_target(target)
    args, params = driver.pop()
    assert args == ()                             # No raw URI is forwarded alongside the parameters.
    assert params["hostaddr"] == address
    assert params["sslmode"] == "disable"
    assert params["passfile"] == os.devnull       # No implicit credentials from .pgpass.
    assert params["gssencmode"] == "disable"
    assert params["prepare_threshold"] is None


def test_remote_driver_receives_only_verified_parameters(driver, ca):
    target = db.parse_target(REMOTE + "?sslmode=verify-full", "test", ca)
    connect_target(target)
    args, params = driver.pop()
    assert args == ()
    assert params["host"] == "remote.invalid" and "hostaddr" not in params
    assert params["sslmode"] == "verify-full" and params["sslrootcert"] == ca
    assert params["gssencmode"] == "disable"
    assert params["passfile"] == os.devnull


@pytest.mark.parametrize("mode", ["disable", "require", "verify-ca", "allow", "prefer"])
def test_remote_weak_tls_is_refused_before_connect(driver, ca, mode):
    with pytest.raises(db.DatabaseConfigError):
        connect_target(db.parse_target(REMOTE + "?sslmode=" + mode, "test", ca))
    assert not driver


def test_conflicting_ca_sources_are_refused(ca):
    with pytest.raises(db.DatabaseConfigError):
        db.parse_target(REMOTE + "?sslrootcert=another-ca.pem", "test", ca)
    assert db.parse_target(REMOTE + "?sslrootcert=" + quote(ca, safe=""), "test", ca).sslrootcert == ca


def test_ca_must_contain_a_readable_ca_certificate(tmp_path):
    path = tmp_path / "invalid-private-path.pem"
    path.write_text("not a certificate", encoding="ascii")
    with pytest.raises(db.DatabaseConfigError) as raised:
        db.parse_target(REMOTE, "test", str(path))
    assert str(path) not in str(raised.value)


def test_ca_is_revalidated_when_the_connection_is_opened(driver, ca):
    from pathlib import Path
    target = db.parse_target(REMOTE, "test", ca)
    Path(ca).write_text("invalidated synthetic certificate", encoding="ascii")
    with pytest.raises(db.DatabaseConfigError):
        connect_target(target)
    assert not driver


def test_hand_constructed_target_cannot_bypass_uri_validation(driver):
    target = db.parse_target(LOCAL, "test")
    forged = replace(target, url=LOCAL + "?host=remote.invalid&sslmode=disable")
    with pytest.raises(db.DatabaseConfigError):
        connect_target(forged)
    assert not driver


def test_target_fields_cannot_weaken_tls(driver, ca):
    target = replace(db.parse_target(REMOTE, "test", ca), host="localhost", sslmode="disable")
    connect_target(target)
    _, params = driver.pop()
    assert params["host"] == "remote.invalid" and params["sslmode"] == "verify-full"


def test_sqlalchemy_rechecks_environment_at_each_connection(monkeypatch, driver):
    pytest.importorskip("sqlalchemy")
    pytest.importorskip("psycopg")
    engine = db.create_engine(db.parse_target(LOCAL, "test"))
    monkeypatch.setenv("PGHOSTADDR", "192.0.2.1")
    try:
        with pytest.raises(db.DatabaseConfigError):
            engine.connect()
    finally:
        engine.dispose()
    assert not driver


def test_connection_metadata_and_server_comment_are_never_public(ca):
    target = db.parse_target(REMOTE, "test", ca)
    for public in (repr(target), target.describe()):
        for value in ("remote.invalid", "synthetic", ca):
            assert value not in public
    with pytest.raises(db.EnvironmentIdentityError) as raised:
        db.check_identity("arbitrary-sensitive-server-comment", "test")
    assert "arbitrary-sensitive" not in str(raised.value)
    assert "DB_ENV_" in str(raised.value)


def test_sqlalchemy_errors_do_not_echo_driver_details():
    pytest.importorskip("sqlalchemy")
    from sqlalchemy.exc import OperationalError
    from tests.test_database_unit import FakeEngine

    class FailedEngine(FakeEngine):
        def connect(self):
            raise OperationalError("", {}, RuntimeError("private-host private-user private-db private-path"))

    with pytest.raises(db.DatabaseConnectionError) as raised:
        with db.read_only_session(db.parse_target(LOCAL, "test"), engine=FailedEngine(None)):
            pytest.fail("connection must fail")
    assert "private-" not in str(raised.value)


def test_existing_custom_connection_timeout_is_preserved(driver):
    target = replace(db.parse_target(LOCAL, "test"), connect_timeout_s=3)
    connect_target(target)
    assert driver[0][1]["connect_timeout"] == 3
