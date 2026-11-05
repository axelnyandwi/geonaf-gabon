"""PostgreSQL/PostGIS connection layer (D2.1): configuration, environment guard, read-only sessions.

Nothing here runs at import time and nothing connects automatically: the Streamlit app keeps its
file storage (``GEONAF_DB_BACKEND=file`` by default). Since D4 the application uses this module only
through ``src.data.storage`` (mode selection, target validation, environment mark) and for its error
types; its PostGIS reads go through ``src.data.postgis_session``. SQLAlchemy and psycopg are imported
only when a PostgreSQL target is parsed or a connection is created (``requirements-db.txt``).

Configuration (read at call time from the environment, never logged):

* ``GEONAF_DB_BACKEND``  ``file`` (default) or ``postgres``: which storage the application selects.
* ``GEONAF_DATABASE_URL`` ``postgresql+psycopg://user:password@host:port/database``.
* ``GEONAF_DB_ENV``       ``dev`` or ``test``: the environment the caller *intends* to reach.

Environment guard. ``GEONAF_DB_ENV`` is only a declaration: a ``.env`` copied from another project
would declare the wrong thing silently. The proof comes from the database itself: each Supabase
project carries a mark written once on the ``geonaf`` schema (``db/supabase/03_mark_environment_*.sql``),
``COMMENT ON SCHEMA geonaf IS 'geonaf-environment:dev'``. Every session reads that mark first and
refuses to hand out the connection unless it equals the declared environment. Any other environment
(``prod`` for instance) is refused before connecting.

Sessions are read-only in D2.1: ``SET TRANSACTION READ ONLY``, a local statement timeout, always
rolled back, connection closed on exit. ``NullPool`` keeps no idle connection on the client side:
pooling is left to the Supabase pooler (Supavisor).
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import os
import ssl
from typing import Iterator, Mapping
from urllib.parse import parse_qsl, unquote, urlsplit

BACKEND_VARIABLE = "GEONAF_DB_BACKEND"
URL_VARIABLE = "GEONAF_DATABASE_URL"
ENV_VARIABLE = "GEONAF_DB_ENV"
SSLROOTCERT_VARIABLE = "GEONAF_DB_SSLROOTCERT"   # CA file (PEM) trusted for remote servers; kept outside the repo.

BACKENDS = ("file", "postgres")
DEFAULT_BACKEND = "file"
ENVIRONMENTS = ("dev", "test")             # No other environment is authorised in D2.1.

SCHEMA = "geonaf"
ENVIRONMENT_MARK = "geonaf-environment:{}"  # Written by db/supabase/03_mark_environment_<env>.sql.
# Tables of the geonaf schema created by migration 0001 as applied on Supabase
# (db/supabase/02_migration_0001_application.sql); a unit test keeps this list in sync with the SQL.
EXPECTED_TABLES = (
    "data_sources", "geometries", "legal_statuses", "licences", "registry_imports", "schema_migrations",
    "source_releases", "territories", "territory_kinds", "territory_versions",
)

DRIVER_SCHEME = "postgresql+psycopg"
ACCEPTED_SCHEMES = ("postgresql", DRIVER_SCHEME)     # postgresql:// is normalised to the psycopg 3 driver.
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")      # Local test databases may run without TLS.
# Remote servers: TLS with the server certificate verified against an explicit CA file AND the host name
# checked (verify-full). "require" encrypts without authenticating the server, "verify-ca" does not
# check the host name: both are refused. libpq 14 (bundled with psycopg 3.2) has no sslrootcert=system,
# so the CA file is always explicit. Local hosts keep their own setting (TLS optional for test servers).
REMOTE_SSLMODE = "verify-full"
CONNECT_TIMEOUT_S = 10
STATEMENT_TIMEOUT_MS = 15_000
APPLICATION_NAME = "geonaf"


class DatabaseConfigError(ValueError):
    """Missing or invalid database configuration; the message never contains the URL."""


class DatabaseConnectionError(RuntimeError):
    """The database could not be reached; the message is redacted."""


class EnvironmentIdentityError(RuntimeError):
    """The database reached is not the declared environment (or carries no mark)."""


def selected_backend(environ: Mapping[str, str] | None = None) -> str:
    """Storage backend chosen by ``GEONAF_DB_BACKEND``; ``file`` when unset."""
    environ = os.environ if environ is None else environ
    backend = environ.get(BACKEND_VARIABLE, "").strip().lower() or DEFAULT_BACKEND
    if backend not in BACKENDS:
        raise DatabaseConfigError(f"{BACKEND_VARIABLE} doit valoir {' ou '.join(BACKENDS)}.")
    return backend


@dataclass(frozen=True)
class DatabaseTarget:
    """A validated connection target. ``url`` holds the password: it is excluded from ``repr``."""

    environment: str
    url: str = field(repr=False)
    host: str = field(repr=False)
    port: int = field(repr=False)
    database: str = field(repr=False)
    user: str = field(repr=False)
    sslmode: str | None                       # None only for a local host without TLS requested.
    sslrootcert: str | None = field(default=None, repr=False)   # CA file path (never shown: local path).
    connect_timeout_s: int = CONNECT_TIMEOUT_S
    statement_timeout_ms: int = STATEMENT_TIMEOUT_MS

    def describe(self) -> str:
        """Public description: environment only, never connection metadata."""
        return f"PostgreSQL (environnement déclaré : {self.environment})"

    @property
    def password(self) -> str:
        parts = urlsplit(self.url)
        return unquote(parts.password or "")


def _refuse_implicit_libpq(environ: Mapping[str, str]) -> None:
    """No ambient PG* configuration: notably hostaddr, service files and TLS overrides.

    Checked again at the driver boundary, including for engines created before the environment changed.
    Never mutate os.environ: other threads may use it. Values and variable names are not echoed.
    """
    if any(key.upper().startswith("PG") and value for key, value in environ.items()):
        raise DatabaseConfigError("DB_CONFIG_IMPLICIT: variables PostgreSQL PG* interdites ; "
                                  "utiliser la configuration GEONAF explicite.")


def _conninfo(url: str) -> dict:
    """Use libpq's parser, lazily and without connecting; no driver exception text escapes."""
    try:
        import psycopg
        from psycopg.conninfo import conninfo_to_dict
    except ImportError:
        raise DatabaseConfigError("psycopg 3 est requis (requirements-db.txt).") from None
    try:
        return conninfo_to_dict(url.replace(DRIVER_SCHEME + "://", "postgresql://", 1))
    except psycopg.Error:
        raise DatabaseConfigError("DB_CONFIG_URI: URL PostgreSQL invalide.") from None


def parse_target(url: str, environment: str, sslrootcert: str | None = None) -> DatabaseTarget:
    """Single explicit TCP destination; only sslmode and sslrootcert are allowed in the query.

    libpq decodes the effective destination. Service files, multi-host URIs and Unix sockets are
    deliberately unsupported. A remote target requires verify-full and a readable PEM CA bundle.
    The same policy is rerun immediately before every physical connection.
    """
    environment = (environment or "").strip().lower()
    if environment not in ENVIRONMENTS:
        raise DatabaseConfigError(f"{ENV_VARIABLE} doit valoir {' ou '.join(ENVIRONMENTS)} ; "
                                  "aucun autre environnement n'est autorisé.")
    _refuse_implicit_libpq(os.environ)
    url = (url or "").strip()
    if not url:
        raise DatabaseConfigError(f"{URL_VARIABLE} n'est pas défini.")
    try:
        parts = urlsplit(url)
        query = parse_qsl(parts.query, keep_blank_values=True, strict_parsing=True) if parts.query else []
    except ValueError:
        raise DatabaseConfigError(f"DB_CONFIG_URI: {URL_VARIABLE} n'est pas une URL PostgreSQL valide.") from None
    if parts.scheme not in ACCEPTED_SCHEMES:
        raise DatabaseConfigError(f"{URL_VARIABLE} doit commencer par {DRIVER_SCHEME}:// "
                                  "(seul le pilote psycopg 3 est pris en charge).")
    if parts.fragment or any(ord(char) < 32 for char in url):
        raise DatabaseConfigError("DB_CONFIG_URI: fragment ou caractère de contrôle interdit.")
    keys = [key for key, _ in query]
    if (len(set(keys)) != len(keys) or any(key not in {"sslmode", "sslrootcert"} or not value
                                         for key, value in query)):
        raise DatabaseConfigError("DB_CONFIG_OPTIONS: seuls sslmode et sslrootcert, uniques et non vides, "
                                  "sont autorisés dans l'URL.")
    info = _conninfo(url)
    host, database = info.get("host", "").lower(), info.get("dbname", "")
    if not host or not database or any(c in host for c in (",", "/", "\\", "@", "%")) or any(c.isspace() for c in host):
        raise DatabaseConfigError("DB_CONFIG_DESTINATION: hôte TCP unique et base explicites exigés ; "
                                  "sockets Unix et destinations multiples non pris en charge.")
    try:
        port = int(info.get("port", "5432"))
        if not 1 <= port <= 65535:
            raise ValueError
    except ValueError:
        raise DatabaseConfigError("DB_CONFIG_DESTINATION: port TCP unique invalide.") from None
    sslmode = info.get("sslmode")
    if sslmode is not None and sslmode not in {"disable", "allow", "prefer", "require", "verify-ca", "verify-full"}:
        raise DatabaseConfigError("DB_CONFIG_TLS: sslmode invalide.")
    uri_ca = info.get("sslrootcert")
    if sslrootcert and uri_ca and os.path.abspath(sslrootcert) != os.path.abspath(uri_ca):
        raise DatabaseConfigError("DB_CONFIG_CA: autorités de certification contradictoires.")
    sslrootcert = sslrootcert or uri_ca
    if host not in LOCAL_HOSTS:
        sslmode = sslmode or REMOTE_SSLMODE
        if sslmode != REMOTE_SSLMODE:
            raise DatabaseConfigError("DB_CONFIG_TLS: une base distante exige sslmode=verify-full.")
    if sslmode in ("verify-ca", "verify-full") and not sslrootcert:
        raise DatabaseConfigError(f"DB_CONFIG_CA: définir {SSLROOTCERT_VARIABLE} (fichier PEM hors dépôt).")
    if sslrootcert:
        try:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.load_verify_locations(cafile=sslrootcert)
            if context.cert_store_stats()["x509_ca"] == 0:
                raise ValueError
        except (OSError, ValueError):
            raise DatabaseConfigError(f"DB_CONFIG_CA: fichier AC illisible ou invalide ({SSLROOTCERT_VARIABLE}).") from None
        sslrootcert = os.path.abspath(sslrootcert)
    normalised = DRIVER_SCHEME + url[len(parts.scheme):]
    return DatabaseTarget(environment=environment, url=normalised, host=host, port=port, database=database,
                          user=info.get("user", ""), sslmode=sslmode, sslrootcert=sslrootcert)


def load_target(environ: Mapping[str, str] | None = None) -> DatabaseTarget:
    """Explicit GEONAF configuration; ambient libpq configuration is refused, never inherited."""
    environ = os.environ if environ is None else environ
    _refuse_implicit_libpq(environ)
    return parse_target(environ.get(URL_VARIABLE, ""), environ.get(ENV_VARIABLE, ""),
                        environ.get(SSLROOTCERT_VARIABLE) or None)


def isolated_test_identity(target: DatabaseTarget) -> str:
    """Require explicit local test authorization; its UUID must also mark the database.

    Never provisions or marks anything. A schema comment or a _test suffix alone
    is not permission to destroy data. The administrator supplies the capability.
    """
    from uuid import UUID

    checked = parse_target(target.url, target.environment, target.sslrootcert)
    token = os.environ.get("GEONAF_TEST_INSTANCE_ID", "")
    try:
        valid_token = str(UUID(token)) == token
    except ValueError:
        valid_token = False
    if (checked.environment != "test" or checked.host not in LOCAL_HOSTS
            or not checked.database.endswith("_test")
            or checked.database != os.environ.get("GEONAF_TEST_DATABASE_NAME")
            or os.environ.get("GEONAF_ALLOW_DESTRUCTIVE_TESTS") != "1" or not valid_token):
        raise DatabaseConfigError("DB_TEST_ISOLATION: base locale dédiée et autorisation explicite requises.")
    return "geonaf-isolated-test:" + token


def check_isolated_test_database(cursor, target: DatabaseTarget) -> None:
    """Read the independent database identity before bootstrap or destructive fixtures."""
    expected = isolated_test_identity(target)
    cursor.execute("SELECT d.datname, pg_catalog.shobj_description(d.oid, 'pg_database') "
                   "FROM pg_catalog.pg_database AS d WHERE d.datname = pg_catalog.current_database()")
    row = cursor.fetchone()
    if not row or row[0] != target.database or row[1] != expected:
        raise EnvironmentIdentityError("DB_TEST_IDENTITY: identité de la base isolée absente ou incompatible.")


def connection_parameters(target: DatabaseTarget) -> dict:
    """Exact psycopg/libpq arguments, revalidated just before connecting (never forward the raw URI).

    localhost is pinned to IPv4 loopback; ::1 stays IPv6. No DNS result can turn a local exemption
    into a remote connection. PG* and service parameters are refused; the default password file is
    disabled. GSS encryption is disabled so a remote connection cannot bypass TLS verification.
    """
    checked = parse_target(target.url, target.environment, target.sslrootcert)
    params = _conninfo(checked.url)
    params.update(host=checked.host, port=str(checked.port), sslmode=checked.sslmode or "disable",
                  passfile=os.devnull, gssencmode="disable", connect_timeout=target.connect_timeout_s,
                  application_name=APPLICATION_NAME, prepare_threshold=None)
    params.update(tls_options(checked))
    if checked.host in LOCAL_HOSTS:
        params["hostaddr"] = "127.0.0.1" if checked.host == "localhost" else checked.host
    return params


def tls_options(target: DatabaseTarget) -> dict:
    """Driver keywords for TLS. Passed as keywords, they override any weaker value left in the URL."""
    options = {}
    if target.sslmode:
        options["sslmode"] = target.sslmode
    if target.sslrootcert:
        options["sslrootcert"] = target.sslrootcert
    return options


def redact(text: str, target: DatabaseTarget) -> str:
    """Remove the URL and the password from a driver message before showing it."""
    for secret in (target.url, target.password):
        if secret:
            text = text.replace(secret, "***")
    return text


def create_engine(target: DatabaseTarget):
    """SQLAlchemy engine for ``target``; creating it opens no connection.

    * ``NullPool``: each session opens and closes its own connection (pooling is the pooler's job).
    * ``prepare_threshold=None``: no server-side prepared statements, which Supavisor's
      transaction mode (port 6543) does not support.
    * ``hide_parameters``: bound values never appear in SQLAlchemy error messages.
    """
    try:
        import psycopg  # noqa: F401  (the dialect needs it; checked here for a clear message)
        from sqlalchemy import create_engine as sqlalchemy_create_engine
        from sqlalchemy.pool import NullPool
    except ImportError:
        raise DatabaseConfigError("SQLAlchemy et psycopg 3 sont requis : pip install -r requirements-db.txt.") from None

    # A creator runs the shared guard at EVERY physical connection, even if PG* changes after
    # engine creation. A credential-free URL also keeps Engine.__repr__ free of connection metadata.
    from src.data.postgis_session import connect_target

    return sqlalchemy_create_engine(DRIVER_SCHEME + "://", poolclass=NullPool, hide_parameters=True,
                                    creator=lambda: connect_target(target))


@contextmanager
def _read_only_connection(target: DatabaseTarget, engine=None) -> Iterator:
    """Connection inside a read-only transaction, without the identity check (diagnostic only).

    The transaction is always rolled back and the connection closed, even on error.
    """
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    owned = engine is None
    engine = create_engine(target) if owned else engine
    try:
        try:
            connection = engine.connect()
        except DBAPIError as error:
            raise DatabaseConnectionError("DB_CONNECT_FAILED: connexion PostgreSQL impossible "
                                          f"({type(error.orig or error).__name__}).") from None
        try:
            connection.begin()
            # Must be the first statement of the transaction. set_config(..., true) is local to the
            # transaction, which the pooler's transaction mode allows (a session-level SET is not).
            connection.execute(text("SET TRANSACTION READ ONLY"))
            connection.execute(text("SELECT set_config('statement_timeout', :timeout, true)"),
                               {"timeout": f"{target.statement_timeout_ms}ms"})
            yield connection
        finally:
            connection.rollback()
            connection.close()
    finally:
        if owned:
            engine.dispose()


def environment_mark(connection) -> str | None:
    """Mark of the ``geonaf`` schema, or None when the schema or the mark is absent."""
    from sqlalchemy import text

    return connection.execute(text("SELECT obj_description(to_regnamespace(:schema), 'pg_namespace')"),
                              {"schema": SCHEMA}).scalar()


def check_identity(mark: str | None, environment: str) -> None:
    """Refuse a database whose schema mark is not the declared environment's."""
    if environment not in ENVIRONMENTS:
        raise DatabaseConfigError("DB_ENV_INVALID: environnement attendu invalide (dev ou test).")
    expected = ENVIRONMENT_MARK.format(environment)
    if mark != expected:
        category = "DB_ENV_MISSING: aucune marque" if mark is None else "DB_ENV_MISMATCH: marque incompatible"
        raise EnvironmentIdentityError(f"{category} ; environnement attendu : {environment}.")


@contextmanager
def read_only_session(target: DatabaseTarget, engine=None) -> Iterator:
    """Read-only connection whose environment mark has been verified; closed and rolled back on exit.

    No connection is handed out before the mark matches ``target.environment``.
    """
    with _read_only_connection(target, engine) as connection:
        check_identity(environment_mark(connection), target.environment)
        yield connection
