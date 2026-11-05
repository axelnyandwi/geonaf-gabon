"""Guarded PostGIS transactions for the application and the imports (R5 bis, D4); no connection at import time.

Two guarantees are given to every transaction that runs a PostGIS function:

* **R5 bis - search path.** The connection's default search path may contain ``public`` (Supabase:
  ``"$user", public, extensions``). A function or type named like a PostGIS one (``st_intersects``,
  ``geometry``) placed in ``public`` would then shadow PostGIS silently. :func:`set_postgis_search_path`
  reads the schema where PostGIS is installed (``pg_extension``) and sets, **local to the transaction**,
  ``pg_catalog, <postgis schema>, pg_temp``: ``public`` is no longer searched (unless PostGIS itself was
  installed there, as on a plain local server; then nothing else can be done), and ``pg_temp`` comes last.
  ``set_config(..., true)`` is allowed by the Supabase pooler in transaction mode, a session ``SET`` is not.
* **Application sessions** (:class:`PostgisSessions`): the connection target is validated by
  :mod:`src.data.database` (TLS for a remote host, declared environment ``dev`` or ``test``), the
  transaction is ``READ ONLY`` with a local statement timeout, the ``geonaf`` schema must carry the mark of
  the declared environment, and the transaction is always rolled back and the connection closed.

* **Connections** (:func:`connect_target`): the only way GEONAF opens a psycopg connection, for the
  application and for the import tools alike. TLS keywords come from the validated target (remote:
  ``verify-full`` with an explicit CA file) and override anything weaker left in the URL.
* **Writes** (:func:`check_environment`): the import functions verify the environment mark inside their
  own transaction, before their first write.

Driver errors are converted to :class:`~src.data.database.DatabaseConnectionError` whose message names
only the kind of failure: no URL, password, host or user (a Supabase host and user name the project).
``psycopg`` is imported inside functions only: the file backend never loads it.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Callable, Iterator

from src.data.database import (
    ENVIRONMENTS, SCHEMA, DatabaseConfigError, DatabaseConnectionError, DatabaseTarget, check_identity, connection_parameters,
)

POSTGIS_SCHEMA_QUERY = ("SELECT n.nspname FROM pg_catalog.pg_extension AS e "
                        "JOIN pg_catalog.pg_namespace AS n ON n.oid = e.extnamespace WHERE e.extname = 'postgis'")
# format('%I') quotes the schema name on the server side; the value is a bound parameter.
SET_SEARCH_PATH = ("SELECT pg_catalog.set_config('search_path', "
                   "pg_catalog.format('pg_catalog, %%I, pg_temp', %s::text), true)")
MARK_QUERY = "SELECT pg_catalog.obj_description(pg_catalog.to_regnamespace(%s), 'pg_namespace')"
TIMEOUT_QUERY = "SELECT pg_catalog.set_config('statement_timeout', %s, true)"


class PostgisUnavailableError(RuntimeError):
    """The PostGIS extension is not installed in the database reached."""


def set_postgis_search_path(cursor) -> str:
    """Set the R5 bis search path for the current transaction; return PostGIS's schema.

    Must run inside a transaction (psycopg opens one implicitly when autocommit is off), before the
    first statement that names a PostGIS function or type.
    """
    cursor.execute(POSTGIS_SCHEMA_QUERY)
    row = cursor.fetchone()
    if not row:
        raise PostgisUnavailableError("Extension PostGIS absente de la base : à activer par un administrateur.")
    schema = row[0] if not isinstance(row, dict) else row["nspname"]
    cursor.execute(SET_SEARCH_PATH, (schema,))
    return schema


def check_environment(cursor, environment: str) -> None:
    """Refuse the database unless its ``geonaf`` schema carries the mark of ``environment``.

    Called inside the transaction that will write, before the first write: an absent, unexpected or
    other environment's mark stops everything (EnvironmentIdentityError).
    """
    if environment not in ENVIRONMENTS:
        raise DatabaseConfigError("Environnement attendu invalide : " + " ou ".join(ENVIRONMENTS) + " seulement.")
    cursor.execute(MARK_QUERY, (SCHEMA,))
    row = cursor.fetchone()
    check_identity(row[0] if row else None, environment)


def connect_target(target: DatabaseTarget):
    """psycopg 3 connection to a validated target (no server-side prepared statements: Supavisor)."""
    import psycopg

    parameters = connection_parameters(target)
    try:
        return psycopg.connect(**parameters)
    except psycopg.Error as error:
        raise DatabaseConnectionError(driver_error_message(error)) from None


class PostgisSessions:
    """Factory of guarded read-only transactions for one validated target."""

    def __init__(self, target: DatabaseTarget, *, connect: Callable | None = None):
        self.target = target
        self._connect = connect or (lambda: connect_target(target))

    def describe(self) -> str:
        return self.target.describe()

    @contextmanager
    def read(self) -> Iterator:
        """Yield a psycopg connection inside a checked READ ONLY transaction; rolled back and closed on exit."""
        try:
            import psycopg
            driver_errors = (psycopg.Error,)
        except ImportError:                     # Fake connections in unit tests.
            driver_errors = ()
        try:
            connection = self._connect()
        except driver_errors as error:
            raise DatabaseConnectionError(self._message(error)) from None
        try:
            connection.read_only = True
            with connection.cursor() as cursor:
                cursor.execute(TIMEOUT_QUERY, (f"{self.target.statement_timeout_ms}ms",))
                check_environment(cursor, self.target.environment)
                set_postgis_search_path(cursor)
            yield connection
        except driver_errors as error:
            raise DatabaseConnectionError(self._message(error)) from None
        finally:
            try:
                connection.rollback()
            finally:
                connection.close()

    def _message(self, error) -> str:
        """User message: no host, user or driver text (they name the Supabase project).

        A refused privilege or a cancelled statement is not an unavailable database: each gets its own
        message. The driver error stays attached as ``__context__`` for the server log, which masks it.
        """
        return driver_error_message(error)


def driver_error_message(error) -> str:
    """Public message for a psycopg error, by SQLSTATE class only (never the driver text)."""
    sqlstate = str(getattr(error, "sqlstate", "") or "")
    if sqlstate == "42501":
        return "Droits insuffisants sur la base PostGIS pour cette lecture (InsufficientPrivilege)."
    if sqlstate == "57014":
        return "Délai maximal de requête dépassé sur la base PostGIS (QueryCanceled)."
    return f"Base PostGIS indisponible ({type(error).__name__}). Réessayer plus tard."


@contextmanager
def plain_read_transaction(connect: Callable) -> Iterator:
    """Read-only transaction with the R5 bis path on a bare connection factory (tools and tests).

    No environment mark is checked here: used by :class:`PostgisTerritoryRegistry` when it is given a
    plain ``connect`` (local test database, import scripts). The application uses :class:`PostgisSessions`.
    """
    connection = connect()
    try:
        connection.read_only = True
        with connection.cursor() as cursor:
            set_postgis_search_path(cursor)
        yield connection
    finally:
        try:
            connection.rollback()
        finally:
            connection.close()
