"""D5: HTTPS reading of the acquisitions through the Edge Function, dedicated role geonaf_edge_reader.

Three levels, none of which touches Supabase or the network:
* unit: SCRAM verifier against RFC 7677, Python client with a fake HTTP session, static consistency
  between db/supabase/13_*.sql and supabase/functions/geonaf-acquisitions/index.ts;
* integration (marker ``postgis``, ``GEONAF_TEST_DATABASE_URL`` on a LOCAL ``*_test`` database, PostGIS
  not needed): scripts 13 run whole as the SQL Editor sends them, on stand-in tables carrying the 0002
  column names; then a real login as the role with the SCRAM verifier and the function's own SQL.
The security helpers of the function itself are tested by tests/frontend/test_edge_acquisitions_security.mjs.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import re

import pytest

from scripts import edge_role_credential as cred
from src.data import edge_acquisitions as edge

ROOT = Path(__file__).resolve().parents[1]
SUPABASE = ROOT / "db" / "supabase"
FUNCTION = ROOT / "supabase" / "functions" / "geonaf-acquisitions" / "index.ts"
ROLE = "geonaf_edge_reader"
TOKEN = "k" * 40
URL = "https://abcdefghijklmnopqrst.supabase.co/functions/v1/geonaf-acquisitions"
SHA = "a" * 64


# --- SCRAM verifier -----------------------------------------------------------------------------------------

def test_scram_verifier_matches_the_rfc_7677_exchange():
    """RFC 7677 section 3: user "user", password "pencil". The verifier alone must validate the client proof
    sent by a real client and reproduce the server signature that client expects."""
    salt = base64.b64decode("W22ZaJ0SNY7soEsUEjb6gQ==")
    verifier = cred.scram_sha256_verifier("pencil", salt=salt)
    match = re.fullmatch(r"SCRAM-SHA-256\$4096:([^$]+)\$([^:]+):(.+)", verifier)
    assert match and base64.b64decode(match.group(1)) == salt
    stored_key, server_key = base64.b64decode(match.group(2)), base64.b64decode(match.group(3))
    nonce = "rOprNGfwEbeRWgbNEkqO%hvYDpWUa2RaTCAfuxFIlj)hNlF$k0"
    auth_message = (f"n=user,r=rOprNGfwEbeRWgbNEkqO,r={nonce},s=W22ZaJ0SNY7soEsUEjb6gQ==,i=4096,"
                    f"c=biws,r={nonce}").encode()
    proof = base64.b64decode("dHzbZapWIk4jUhN+Ute9ytag9zjfMHgsqmmiz7AndVQ=")
    client_signature = hmac.new(stored_key, auth_message, "sha256").digest()
    client_key = bytes(a ^ b for a, b in zip(proof, client_signature))
    assert hashlib.sha256(client_key).digest() == stored_key
    assert base64.b64encode(hmac.new(server_key, auth_message, "sha256").digest()).decode() == \
        "6rriTRBi23WpRR/wtup+mMhUZUn/dB5nLTJRsjl95G4="


def test_login_statement_contains_the_verifier_only():
    verifier = cred.scram_sha256_verifier("p" * 40, salt=bytes(16))
    statement = cred.login_statement(verifier)
    assert statement == f"ALTER ROLE geonaf_edge_reader WITH LOGIN PASSWORD '{verifier}';"
    assert "p" * 40 not in statement
    for bad in ("p" * 40, "md5abc", verifier + "'; DROP ROLE x; --", verifier.replace("4096", "1")):
        with pytest.raises(ValueError):
            cred.login_statement(bad)


def test_credential_tool_never_prints_the_password(monkeypatch, capsys):
    secret = "Z9_-" * 10
    answers = iter([secret, secret])
    monkeypatch.setattr(cred.getpass, "getpass", lambda prompt="": next(answers))
    assert cred.main() == 0
    out, err = capsys.readouterr()
    assert secret not in out + err and out.startswith("ALTER ROLE geonaf_edge_reader WITH LOGIN PASSWORD 'SCRAM-")


@pytest.mark.parametrize("pair", [("short", "short"), ("a" * 40 + "%", "a" * 40 + "%"), ("a" * 40, "b" * 40)])
def test_credential_tool_refuses_weak_or_mismatched_passwords(monkeypatch, capsys, pair):
    answers = iter(pair)
    monkeypatch.setattr(cred.getpass, "getpass", lambda prompt="": next(answers))
    assert cred.main() == 2
    out, err = capsys.readouterr()
    assert out == "" and pair[0] not in err


# --- Python client -------------------------------------------------------------------------------------------

def record(**changes):
    value = {"manifest_sha256": SHA, "collection": "sentinel-2-l2a", "product_id": "S2B_MSIL2A_20250125",
             "acquired_at": "2025-01-25T09:31:59.000Z", "ingested_at": "2026-10-20T08:00:00.000Z",
             "grid_crs": "EPSG:32632", "grid_resolution_m": 10, "integrity_mode": "strict"}
    value.update(changes)
    return value


def payload(*records):
    return {"environment": "test", "count": len(records), "acquisitions": list(records)}


class FakeResponse:
    def __init__(self, status=200, body=b"", content_type="application/json; charset=utf-8"):
        self.status_code, self.body, self.headers = status, body, {"content-type": content_type}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def iter_content(self, size):
        for start in range(0, len(self.body), size):
            yield self.body[start:start + size]


class FakeSession:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.calls = response, error, []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.error:
            raise self.error
        return self.response


def config():
    return edge.EdgeConfig(url=URL, token=TOKEN)


def test_config_from_environment_and_masked_repr():
    loaded = edge.EdgeConfig.from_environment({"GEONAF_EDGE_ACQUISITIONS_URL": URL + "/",
                                               "GEONAF_EDGE_TOKEN": TOKEN})
    assert loaded == config() and TOKEN not in repr(loaded)


@pytest.mark.parametrize("url", ["", "http://abcdefghijklmnopqrst.supabase.co/functions/v1/geonaf-acquisitions",
                                 "https://evil.example/functions/v1/geonaf-acquisitions",
                                 "https://abc.supabase.co.evil.example/functions/v1/geonaf-acquisitions",
                                 "https://u:p@abc.supabase.co/functions/v1/geonaf-acquisitions",
                                 "https://abc.supabase.co:8443/functions/v1/geonaf-acquisitions",
                                 "https://abc.supabase.co/functions/v1/other",
                                 URL + "?x=1", URL + "#x"])
def test_config_refuses_other_urls(url):
    with pytest.raises(edge.EdgeAcquisitionsError) as error:
        edge.EdgeConfig.from_environment({"GEONAF_EDGE_ACQUISITIONS_URL": url, "GEONAF_EDGE_TOKEN": TOKEN})
    assert error.value.category == "configuration"


@pytest.mark.parametrize("token", ["", "x" * 31, " " + "x" * 40, "x" * 40 + "\n"])
def test_config_refuses_weak_tokens_without_echoing_them(token):
    with pytest.raises(edge.EdgeAcquisitionsError) as error:
        edge.EdgeConfig.from_environment({"GEONAF_EDGE_ACQUISITIONS_URL": URL, "GEONAF_EDGE_TOKEN": token})
    assert token.strip() == "" or token.strip() not in str(error.value)


def test_fetch_sends_the_token_without_following_redirects_and_parses_records():
    session = FakeSession(FakeResponse(body=json.dumps(payload(record(), record(integrity_mode=None))).encode()))
    rows = edge.fetch_acquisitions(config(), session=session)
    url, kwargs = session.calls[0]
    assert url == URL and kwargs["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert kwargs["allow_redirects"] is False and kwargs["stream"] is True and kwargs["timeout"]
    assert len(rows) == 2 and rows[0].acquired_at.isoformat() == "2025-01-25T09:31:59+00:00"
    assert rows[0].ingested_at > rows[0].acquired_at and rows[1].integrity_mode is None


@pytest.mark.parametrize("status,category", [(401, "unauthorized"), (429, "busy"), (503, "unavailable"),
                                             (302, "http"), (500, "http")])
def test_fetch_maps_http_errors_without_body_or_url(status, category):
    session = FakeSession(FakeResponse(status=status, body=b'{"error":"db.secret.host"}'))
    with pytest.raises(edge.EdgeAcquisitionsError) as error:
        edge.fetch_acquisitions(config(), session=session)
    assert error.value.category == category
    assert "secret" not in str(error.value) and "supabase" not in str(error.value) and TOKEN not in str(error.value)


def test_fetch_caps_the_body_and_requires_json():
    big = FakeResponse(body=b" " * (edge.MAX_BODY_BYTES + 1))
    with pytest.raises(edge.EdgeAcquisitionsError, match="volumineuse"):
        edge.fetch_acquisitions(config(), session=FakeSession(big))
    html = FakeResponse(body=b"<html>", content_type="text/html")
    with pytest.raises(edge.EdgeAcquisitionsError, match="non JSON"):
        edge.fetch_acquisitions(config(), session=FakeSession(html))


def test_fetch_classifies_transport_errors_without_details():
    import requests

    for error, category in ((requests.exceptions.SSLError("cert for db.x"), "tls"),
                            (requests.exceptions.ConnectTimeout("x"), "timeout"),
                            (requests.exceptions.ConnectionError("x"), "connection")):
        with pytest.raises(edge.EdgeAcquisitionsError) as raised:
            edge.fetch_acquisitions(config(), session=FakeSession(error=error))
        assert raised.value.category == category and "db.x" not in str(raised.value)
        assert raised.value.__cause__ is None and raised.value.__suppress_context__


@pytest.mark.parametrize("bad", [
    {"environment": "dev", "count": 0, "acquisitions": []},
    {"environment": "test", "count": 1, "acquisitions": []},
    {"environment": "test", "count": 0, "acquisitions": [], "extra": 1},
    payload(*[record()] * (edge.MAX_RECORDS + 1)),
    payload(record(manifest_document={})),
    payload({k: v for k, v in record().items() if k != "grid_crs"}),
    payload(record(manifest_sha256="A" * 64)),
    payload(record(acquired_at="2025-01-25T09:31:59")),          # naive date refused
    payload(record(ingested_at="2025-01-01T00:00:00Z")),          # ingestion before acquisition
    payload(record(grid_resolution_m=0)), payload(record(grid_resolution_m=True)),
    payload(record(integrity_mode="other")),
    [],
])
def test_parse_payload_is_strict(bad):
    with pytest.raises(edge.EdgeAcquisitionsError) as error:
        edge.parse_payload(bad)
    assert error.value.category == "protocol"


def test_client_is_not_wired_into_the_default_storage():
    """File storage stays the default: the HTTPS client is reached only through the explicit Supabase panel
    (pages > components > services > data), never by the storage layer or the data-source selection."""
    allowed = {"src/services/supabase_acquisitions.py",
               "src/components/supabase_acquisitions.py",
               "src/components/errors.py"}                      # Domain error type only, for safe display.
    users = {path.relative_to(ROOT).as_posix()
             for path in [ROOT / "app.py", *(ROOT / "src").rglob("*.py"), *(ROOT / "views").rglob("*.py")]
             if "edge_acquisitions" in path.read_text(encoding="utf-8")}
    assert users == allowed
    errors_source = (ROOT / "src" / "components" / "errors.py").read_text(encoding="utf-8")
    assert "from src.data.edge_acquisitions import EdgeAcquisitionsError\n" in errors_source
    panel_users = {path.relative_to(ROOT).as_posix() for path in [*(ROOT / "views").rglob("*.py"), ROOT / "app.py"]
                   if "supabase_acquisitions" in path.read_text(encoding="utf-8")}
    assert panel_users == {"views/analyses_temporelles.py"}


# --- Static consistency of the SQL scripts and the function -------------------------------------------------

def script(name):
    return (SUPABASE / name).read_text(encoding="utf-8")


def sql_list(text, name):
    value = re.search(rf"{name} constant text :=\s*'([^']+)'", text).group(1)
    return {column.strip() for column in value.split(",")}


def test_granted_columns_are_exactly_those_the_function_reads():
    text = script("13_edge_reader_role.sql")
    granted = {("acquisitions", c) for c in sql_list(text, "acquisition_columns")} | \
              {("acquisition_manifests", c) for c in sql_list(text, "manifest_columns")}
    query = re.search(r"SELECT m\.manifest_sha256.*?LIMIT 100", FUNCTION.read_text(encoding="utf-8"), re.S).group(0)
    aliases = {"m": "acquisition_manifests", "a": "acquisitions"}
    used = {(aliases[a], c) for a, c in re.findall(r"\b([am])\.([a-z_0-9]+)", query)}
    assert used == granted
    verify = script("13_edge_reader_role_verify.sql")
    block = re.search(r"granted\(table_name, column_name\) AS \(VALUES(.*?)\n\)", verify, re.S).group(1)
    assert set(re.findall(r"\('(acquisitions|acquisition_manifests)', '([a-z_0-9]+)'\)", block)) == granted
    assert "colonnes attendues lisibles (10)', '10'" in verify and len(granted) == 10


@pytest.mark.parametrize("name", ["13_edge_reader_role.sql", "13_edge_reader_role_rollback.sql"])
def test_edge_scripts_simulate_by_default_and_write_only_through_format_templates(name):
    text = script(name)
    assert "application constant boolean := false;" in text
    assert re.search(r"IF NOT application THEN\s+RAISE EXCEPTION 'GEONAF: SIMULATION REUSSIE", text)
    assert not re.search(r"PASSWORD", re.sub(r"--[^\n]*", "", text), re.I)
    # A write keyword selected alone must be a syntax error: it only appears in a format() template with %I.
    for line in re.sub(r"--[^\n]*", "", text).splitlines():
        if re.search(r"\b(CREATE ROLE|ALTER ROLE|DROP ROLE|GRANT|REVOKE|CREATE POLICY|DROP POLICY)\b", line):
            assert "format('" in line and "%I" in line, line


def test_function_uses_only_the_dedicated_role_and_verified_tls():
    source = FUNCTION.read_text(encoding="utf-8")
    assert "GEONAF_EDGE_DATABASE_URL" in source and "SUPABASE_DB_URL')" not in source
    assert "connectionString" not in source and "rejectUnauthorized: true" in source and "ca," in source
    assert "BEGIN READ ONLY" in source and "SET LOCAL statement_timeout" in source
    assert re.search(r"\$\d", source) is None            # No parameters: never a prepared statement (pooler).
    assert "console.error(`geonaf-acquisitions database_unavailable code=${safeErrorCode(error)}`)" in source
    assert source.count("console.") == 1
    # Abuse guard order: authentication, then memo, then the concurrency slot, released in finally only.
    auth, memo, enter = (source.index(s) for s in ("await authorized(", "gate.cached()", "gate.tryEnter()"))
    assert auth < memo < enter < source.index("new Client(") < source.index("finally {") < source.index("gate.leave()")
    assert source.count("gate.leave()") == 1 and source.count("gate.store(") == 1
    assert source.index("gate.store(") > source.index("identityAccepted(")      # Only a verified success.
    security = (FUNCTION.parent / "security.mjs").read_text(encoding="utf-8")
    assert f"EDGE_ROLE = '{ROLE}'" in security and "geonaf_reader'" not in security


# --- Integration on a local PostgreSQL (never Supabase) -----------------------------------------------------

TEST_URL = os.environ.get("GEONAF_TEST_DATABASE_URL", "").strip()
PASSWORD = "Edge_test_password_0123456789_abcdef"
STAND_INS = """
CREATE SCHEMA geonaf;
CREATE TABLE geonaf.data_imports (import_id bigint PRIMARY KEY, source_label text NOT NULL);
CREATE TABLE geonaf.acquisitions (collection text, product_id text, acquired_at timestamptz NOT NULL,
  PRIMARY KEY (collection, product_id));
CREATE TABLE geonaf.acquisition_manifests (manifest_sha256 char(64) PRIMARY KEY, collection text NOT NULL,
  product_id text NOT NULL, schema_version text NOT NULL, acquired_at timestamptz NOT NULL,
  ingested_at timestamptz NOT NULL, stac_source text NOT NULL, grid_crs text NOT NULL,
  grid_resolution_m double precision NOT NULL, footprint text, integrity_mode text, manifest_document jsonb NOT NULL,
  import_id bigint NOT NULL REFERENCES geonaf.data_imports, FOREIGN KEY (collection, product_id)
  REFERENCES geonaf.acquisitions);
CREATE TABLE geonaf.indicator_results (result_id bigint PRIMARY KEY, value double precision);
CREATE FUNCTION geonaf.forbid_change() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RETURN NULL; END $$;
REVOKE ALL ON ALL TABLES IN SCHEMA geonaf FROM PUBLIC;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA geonaf FROM PUBLIC;
REVOKE ALL ON SCHEMA geonaf FROM PUBLIC;
ALTER TABLE geonaf.data_imports ENABLE ROW LEVEL SECURITY;
ALTER TABLE geonaf.acquisitions ENABLE ROW LEVEL SECURITY;
ALTER TABLE geonaf.acquisition_manifests ENABLE ROW LEVEL SECURITY;
ALTER TABLE geonaf.indicator_results ENABLE ROW LEVEL SECURITY;
INSERT INTO geonaf.data_imports VALUES (1, 'test');
INSERT INTO geonaf.acquisitions VALUES ('sentinel-2-l2a', 'S2_A', '2025-01-15T09:32:00Z'),
  ('sentinel-2-l2a', 'S2_B', '2025-01-25T09:32:00Z');
INSERT INTO geonaf.acquisition_manifests VALUES
  (repeat('a', 64), 'sentinel-2-l2a', 'S2_A', 'v1', '2025-01-15T09:32:00Z', '2026-10-20T08:00:00Z', 'https://stac',
   'EPSG:32632', 10, NULL, 'strict', '{"secret": "document"}', 1),
  (repeat('b', 64), 'sentinel-2-l2a', 'S2_B', 'v1', '2025-01-25T09:32:00Z', '2026-10-20T08:00:00Z', 'https://stac',
   'EPSG:32632', 10, NULL, NULL, '{"secret": "document"}', 1);
COMMENT ON SCHEMA geonaf IS 'geonaf-environment:test';
"""


def conninfo() -> str:
    return "postgresql://" + TEST_URL.split("://", 1)[1] if TEST_URL.startswith("postgresql+psycopg://") else TEST_URL


def run(c, text: str):
    """The whole script in one simple-protocol request, as the SQL Editor sends it; last result returned."""
    cursor = c.execute(text.encode("utf-8"))
    columns, rows = None, None
    while True:
        if cursor.description:
            columns, rows = [d.name for d in cursor.description], cursor.fetchall()
        if not cursor.nextset():
            return columns, rows


def applied(name):
    return script(name).replace("application constant boolean := false;", "application constant boolean := true;")


def checks(c):
    columns, rows = run(c, script("13_edge_reader_role_verify.sql"))
    i = {n: columns.index(n) for n in ("ordre", "obtenu", "conforme")}
    return {r[i["ordre"]]: (r[i["obtenu"]], r[i["conforme"]]) for r in rows}


def function_queries():
    source = FUNCTION.read_text(encoding="utf-8")
    sets = [m.group(2) for m in re.finditer(r"client\.query\((['\"])(SET LOCAL .+?)\1\)", source)]
    selects = re.findall(r"client\.query\(\s*`(.*?)`", source, re.S)
    assert len(sets) == 3 and len(selects) == 2
    return sets, selects


@pytest.fixture
def db():
    if not TEST_URL:
        pytest.skip("GEONAF_TEST_DATABASE_URL non défini : base locale requise.")
    psycopg = pytest.importorskip("psycopg")
    from psycopg.conninfo import conninfo_to_dict

    info = conninfo_to_dict(conninfo())
    if not str(info.get("dbname", "")).endswith("_test") or info.get("host") not in ("127.0.0.1", "localhost", "::1"):
        pytest.fail("Base locale dont le nom se termine par « _test » exigée.")
    with psycopg.connect(conninfo(), autocommit=True) as c:
        if c.execute("SELECT count(*) FROM pg_roles WHERE rolname IN (%s, 'geonaf_edge_probe')",
                     (ROLE,)).fetchone()[0]:
            pytest.fail("Rôle Edge déjà présent sur le serveur local : nettoyer avant de tester 13.")
        if not c.execute("SELECT rolsuper OR rolcreaterole FROM pg_roles WHERE rolname = current_user").fetchone()[0]:
            pytest.skip("CREATEROLE requis sur le serveur local pour tester 13.")
        c.execute("DROP SCHEMA IF EXISTS geonaf CASCADE")
        run(c, STAND_INS)
        try:
            yield c
        finally:
            c.execute("DROP SCHEMA IF EXISTS geonaf CASCADE")
            for role in (ROLE, "geonaf_edge_probe"):
                if c.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone():
                    c.execute(f"DROP OWNED BY {role}")
                    c.execute(f"DROP ROLE {role}")


def role_exists(c) -> bool:
    return c.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (ROLE,)).fetchone() is not None


integration = pytest.mark.postgis


@integration
def test_13_simulation_keeps_nothing(db):
    import psycopg

    with pytest.raises(psycopg.errors.RaiseException, match="SIMULATION REUSSIE"):
        run(db, script("13_edge_reader_role.sql"))
    assert not role_exists(db)


@integration
def test_13_refuses_an_unmarked_project(db):
    import psycopg

    db.execute("COMMENT ON SCHEMA geonaf IS 'geonaf-environment:dev'")
    with pytest.raises(psycopg.errors.RaiseException, match="non marque"):
        run(db, applied("13_edge_reader_role.sql"))
    assert not role_exists(db)


@integration
def test_13_application_is_idempotent_and_verified(db):
    run(db, applied("13_edge_reader_role.sql"))
    run(db, applied("13_edge_reader_role.sql"))
    result = checks(db)
    assert result[2] == ("false", None)                    # Still NOLOGIN before the credential step.
    failed = {k: v for k, v in result.items() if v[1] is False}
    information = {2, 31, 32, 33, 34}
    assert not failed and all(v[1] is True for k, v in result.items() if k not in information)
    assert all(result[k][1] is None for k in information) and result[30] == ("aucun", True)
    assert result[31][0] in ("true", "false") and result[34][0] == "aucune"


@integration
def test_13_refuses_a_role_member_of_another_role(db):
    import psycopg

    run(db, applied("13_edge_reader_role.sql"))
    db.execute("CREATE ROLE geonaf_edge_probe NOLOGIN")
    db.execute(f"GRANT geonaf_edge_probe TO {ROLE}")
    with pytest.raises(psycopg.errors.RaiseException, match="membre d''un autre role|membre d'un autre role"):
        run(db, applied("13_edge_reader_role.sql"))
    assert checks(db)[4] == ("1", False)


@integration
def test_edge_role_login_and_function_queries(db):
    """Real SCRAM login with the verifier, then the function's own SQL: scoped columns only, RLS by the
    role's own policies (no inheritance), read only, role defaults applied at session start."""
    import psycopg

    run(db, applied("13_edge_reader_role.sql"))
    run(db, cred.login_statement(cred.scram_sha256_verifier(PASSWORD, salt=os.urandom(16))))
    assert checks(db)[2] == ("true", None)
    info = psycopg.conninfo.conninfo_to_dict(conninfo())
    edge_info = {k: v for k, v in info.items() if k in ("host", "port", "dbname", "hostaddr")}
    sets, (identity_sql, acquisitions_sql) = function_queries()
    with psycopg.connect(**edge_info, user=ROLE, password=PASSWORD, autocommit=True) as c:
        assert c.execute("SHOW statement_timeout").fetchone()[0] == "5s"
        assert c.execute("SHOW default_transaction_read_only").fetchone()[0] == "on"
        c.execute("BEGIN READ ONLY")
        for statement in sets:
            c.execute(statement)
        row = c.execute(identity_sql).fetchone()
        assert row == (ROLE, False, False, "on", "geonaf-environment:test")
        cursor = c.execute(acquisitions_sql)
        assert [d.name for d in cursor.description] == [
            "manifest_sha256", "collection", "product_id", "acquired_at", "ingested_at", "grid_crs",
            "grid_resolution_m", "integrity_mode"]
        assert [r[2] for r in cursor.fetchall()] == ["S2_B", "S2_A"]
        c.execute("ROLLBACK")
        for forbidden in ("SELECT manifest_document FROM geonaf.acquisition_manifests",
                          "SELECT stac_source FROM geonaf.acquisition_manifests",
                          "SELECT * FROM geonaf.acquisition_manifests",
                          "SELECT * FROM geonaf.data_imports", "SELECT * FROM geonaf.indicator_results",
                          "SELECT geonaf.forbid_change()"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                c.execute(forbidden)
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            c.execute("INSERT INTO geonaf.acquisitions VALUES ('x', 'y', now())")
        # Read only is a session default the role could lift: privileges must refuse the write on their own.
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with c.transaction():
                c.execute("SET LOCAL transaction_read_only = off")
                c.execute("INSERT INTO geonaf.acquisitions VALUES ('x', 'y', now())")
    # RLS: without the role's own policy the table is empty for it (default deny), whatever other policies exist.
    db.execute("DROP POLICY geonaf_edge_reader_select ON geonaf.acquisitions")
    with psycopg.connect(**edge_info, user=ROLE, password=PASSWORD, autocommit=True) as c:
        assert c.execute("SELECT count(*) FROM geonaf.acquisitions").fetchone()[0] == 0


@integration
def test_edge_role_wrong_password_is_refused_when_the_server_checks_passwords(db):
    import psycopg

    run(db, applied("13_edge_reader_role.sql"))
    run(db, cred.login_statement(cred.scram_sha256_verifier(PASSWORD, salt=os.urandom(16))))
    info = psycopg.conninfo.conninfo_to_dict(conninfo())
    edge_info = {k: v for k, v in info.items() if k in ("host", "port", "dbname", "hostaddr")}
    try:
        psycopg.connect(**edge_info, user=ROLE, password="wrong" + PASSWORD).close()
    except psycopg.OperationalError:
        return
    pytest.skip("Le serveur local n'exige pas de mot de passe (pg_hba trust) : refus non vérifiable ici.")


@integration
def test_13_nologin_role_cannot_connect(db):
    import psycopg

    run(db, applied("13_edge_reader_role.sql"))
    info = psycopg.conninfo.conninfo_to_dict(conninfo())
    edge_info = {k: v for k, v in info.items() if k in ("host", "port", "dbname", "hostaddr")}
    with pytest.raises(psycopg.OperationalError):
        psycopg.connect(**edge_info, user=ROLE, password=PASSWORD).close()


@integration
def test_13_rollback_simulates_then_removes_only_the_edge_objects(db):
    import psycopg

    run(db, applied("13_edge_reader_role.sql"))
    with pytest.raises(psycopg.errors.RaiseException, match="SIMULATION REUSSIE"):
        run(db, script("13_edge_reader_role_rollback.sql"))
    assert role_exists(db)
    run(db, applied("13_edge_reader_role_rollback.sql"))
    assert not role_exists(db)
    assert db.execute("SELECT count(*) FROM pg_policies WHERE schemaname = 'geonaf'").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM geonaf.acquisition_manifests").fetchone()[0] == 2


# --- Secrets never trackable by Git --------------------------------------------------------------------------

SECRET_PATHS = (".env", ".env.local", "supabase/.env", "supabase/functions/.env",
                "supabase/functions/geonaf-acquisitions/.env", "supabase/functions/geonaf-acquisitions/.env.local",
                "supabase/.temp/project-ref", "supabase/.temp/pooler-url", "supabase/.branches/_current_branch",
                "prod-ca-2021.crt", "supabase/prod-ca-2021.crt", "edge.pem", "edge.key", ".streamlit/secrets.toml",
                "secrets/edge_token.txt")
TRACKED_SOURCES = ("supabase/functions/geonaf-acquisitions/index.ts",
                   "supabase/functions/geonaf-acquisitions/security.mjs", ".env.example",
                   "db/supabase/13_edge_reader_role.sql")


def test_secret_files_are_ignored_and_sources_are_not():
    import shutil

    if shutil.which("git") is None or not (ROOT / ".git").exists():
        pytest.skip("git ou dépôt absent : règles .gitignore non vérifiables ici.")
    import subprocess

    ignored = subprocess.run(["git", "check-ignore", "--no-index", *SECRET_PATHS], cwd=ROOT,
                             capture_output=True, text=True, timeout=30).stdout.split()
    assert sorted(ignored) == sorted(SECRET_PATHS)
    kept = subprocess.run(["git", "check-ignore", "--no-index", *TRACKED_SOURCES], cwd=ROOT,
                          capture_output=True, text=True, timeout=30).stdout.split()
    assert kept == []
    tracked = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, timeout=30).stdout
    assert not re.search(r"(^|/)(\.env(\.(?!example$)[^/]*)?|[^/]+\.(pem|key|crt))$", tracked, re.M)
