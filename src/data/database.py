"""PostgreSQL/PostGIS connection layer (D2.1): configuration, environment guard, read-only sessions.

Nothing here runs at import time and nothing connects automatically: the Streamlit app keeps its
file storage (``GEONAF_DB_BACKEND=file`` by default) and never imports this module today.
SQLAlchemy and psycopg are imported only when an engine is created (``requirements-db.txt``).

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
from typing import Iterator, Mapping
from urllib.parse import parse_qs, unquote, urlsplit

BACKEND_VARIABLE = "GEONAF_DB_BACKEND"
URL_VARIABLE = "GEONAF_DATABASE_URL"
ENV_VARIABLE = "GEONAF_DB_ENV"

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
SECURE_SSLMODES = ("require", "verify-ca", "verify-full")
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
    host: str
    port: int
    database: str
    user: str
    sslmode: str | None                       # None only for a local host without TLS requested.
    connect_timeout_s: int = CONNECT_TIMEOUT_S
    statement_timeout_ms: int = STATEMENT_TIMEOUT_MS

    def describe(self) -> str:
        """Masked description for logs and reports: no password, no query string."""
        return f"{self.user or '?'}@{self.host}:{self.port}/{self.database} (environnement déclaré : {self.environment})"

    @property
    def password(self) -> str:
        parts = urlsplit(self.url)
        return unquote(parts.password or "")


def parse_target(url: str, environment: str) -> DatabaseTarget:
    """Validate a URL and a declared environment without connecting; errors never echo the URL."""
    environment = (environment or "").strip().lower()
    if environment not in ENVIRONMENTS:
        raise DatabaseConfigError(f"{ENV_VARIABLE} doit valoir {' ou '.join(ENVIRONMENTS)} ; "
                                  "aucun autre environnement n'est autorisé.")
    url = (url or "").strip()
    if not url:
        raise DatabaseConfigError(f"{URL_VARIABLE} n'est pas défini.")
    try:
        parts = urlsplit(url)
        port = parts.port or 5432
    except ValueError:
        raise DatabaseConfigError(f"{URL_VARIABLE} n'est pas une URL PostgreSQL valide.") from None
    if parts.scheme not in ACCEPTED_SCHEMES:
        raise DatabaseConfigError(f"{URL_VARIABLE} doit commencer par {DRIVER_SCHEME}:// "
                                  "(seul le pilote psycopg 3 est pris en charge).")
    host = parts.hostname or ""
    database = unquote(parts.path.lstrip("/"))
    if not host or not database:
        raise DatabaseConfigError(f"{URL_VARIABLE} doit indiquer l'hôte et la base de données.")
    sslmode = (parse_qs(parts.query).get("sslmode") or [None])[-1]
    if host not in LOCAL_HOSTS:
        sslmode = sslmode or "require"
        if sslmode not in SECURE_SSLMODES:
            raise DatabaseConfigError("Une base distante exige TLS : sslmode doit valoir "
                                      + ", ".join(SECURE_SSLMODES) + ".")
    normalised = DRIVER_SCHEME + url[len(parts.scheme):]
    return DatabaseTarget(environment=environment, url=normalised, host=host, port=port, database=database,
                          user=unquote(parts.username or ""), sslmode=sslmode)


def load_target(environ: Mapping[str, str] | None = None) -> DatabaseTarget:
    """Target from ``GEONAF_DATABASE_URL`` and ``GEONAF_DB_ENV``; independent of the selected backend."""
    environ = os.environ if environ is None else environ
    return parse_target(environ.get(URL_VARIABLE, ""), environ.get(ENV_VARIABLE, ""))


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
        from sqlalchemy.exc import ArgumentError
        from sqlalchemy.pool import NullPool
    except ImportError:
        raise DatabaseConfigError("SQLAlchemy et psycopg 3 sont requis : pip install -r requirements-db.txt.") from None

    connect_args = {"connect_timeout": target.connect_timeout_s, "prepare_threshold": None,
                    "application_name": APPLICATION_NAME}
    if target.sslmode:
        connect_args["sslmode"] = target.sslmode
    try:
        return sqlalchemy_create_engine(target.url, poolclass=NullPool, hide_parameters=True,
                                        connect_args=connect_args)
    except ArgumentError:   # Its message would contain the whole URL, password included.
        raise DatabaseConfigError(f"{URL_VARIABLE} n'est pas une URL SQLAlchemy valide.") from None


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
            message = redact(str(error.orig or error).strip().splitlines()[0], target)
            raise DatabaseConnectionError(f"Connexion impossible à {target.describe()} : "
                                          f"{type(error.orig or error).__name__} : {message}") from None
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
    expected = ENVIRONMENT_MARK.format(environment)
    if mark != expected:
        found = "aucune marque" if mark is None else f"« {mark} »"
        raise EnvironmentIdentityError(f"La base n'est pas l'environnement « {environment} » : marque attendue "
                                       f"« {expected} », trouvée : {found}.")


@contextmanager
def read_only_session(target: DatabaseTarget, engine=None) -> Iterator:
    """Read-only connection whose environment mark has been verified; closed and rolled back on exit.

    No connection is handed out before the mark matches ``target.environment``.
    """
    with _read_only_connection(target, engine) as connection:
        check_identity(environment_mark(connection), target.environment)
        yield connection
