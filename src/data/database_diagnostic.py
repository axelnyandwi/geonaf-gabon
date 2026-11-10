"""Read-only diagnostic of a PostgreSQL/PostGIS target (D2.1); writes nothing, creates nothing.

Checks, in order: connection, ``geonaf`` schema, environment mark, expected tables, PostGIS. When a
check fails, the following ones are reported as « non exécuté »: in particular nothing is read from
the schema of a database whose identity is not confirmed.

:func:`run_checks` is pure: it receives a ``fetch(sql, params) -> list[tuple]`` function, so unit
tests replace the database by a dictionary of answers. :func:`diagnose` binds it to a real
read-only transaction.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Sequence

from src.data.database import (
    EXPECTED_TABLES, SCHEMA, DatabaseConnectionError, DatabaseTarget, EnvironmentIdentityError,
    _read_only_connection, check_identity,
)

OK, FAILED, SKIPPED = "ok", "échec", "non exécuté"

CONNECTION_SQL = "SELECT current_database(), current_setting('server_version')"
SCHEMA_SQL = "SELECT to_regnamespace(%(schema)s) IS NOT NULL"
MARK_SQL = "SELECT obj_description(to_regnamespace(%(schema)s), 'pg_namespace')"
TABLES_SQL = ("SELECT relname FROM pg_class WHERE relnamespace = to_regnamespace(%(schema)s) "
              "AND relkind IN ('r', 'p') ORDER BY relname")
POSTGIS_SQL = ("SELECT e.extversion, n.nspname FROM pg_extension e "
               "JOIN pg_namespace n ON n.oid = e.extnamespace WHERE e.extname = 'postgis'")

Fetch = Callable[[str, dict], Sequence[tuple]]


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str


@dataclass(frozen=True)
class DiagnosticReport:
    target: str                # Masked description, safe to print.
    checks: tuple[Check, ...]

    @property
    def ok(self) -> bool:
        return all(check.status == OK for check in self.checks)


def quote_identifier(name: str) -> str:
    """SQL identifier in double quotes (the PostGIS schema name comes from the catalogue)."""
    return '"' + name.replace('"', '""') + '"'


def _check_connection(fetch: Fetch, environment: str) -> str:
    database, version = fetch(CONNECTION_SQL, {})[0]
    return f"PostgreSQL {version}, base « {database} »"


def _check_schema(fetch: Fetch, environment: str) -> str:
    if not fetch(SCHEMA_SQL, {"schema": SCHEMA})[0][0]:
        raise LookupError(f"schéma « {SCHEMA} » absent")
    return f"schéma « {SCHEMA} » présent"


def _check_identity(fetch: Fetch, environment: str) -> str:
    try:
        check_identity(fetch(MARK_SQL, {"schema": SCHEMA})[0][0], environment)
    except EnvironmentIdentityError as error:
        raise LookupError(str(error)) from None
    return f"marque « geonaf-environment:{environment} » confirmée par la base"


def _check_tables(fetch: Fetch, environment: str) -> str:
    found = {row[0] for row in fetch(TABLES_SQL, {"schema": SCHEMA})}
    missing, extra = sorted(set(EXPECTED_TABLES) - found), sorted(found - set(EXPECTED_TABLES))
    if missing:
        raise LookupError("tables absentes : " + ", ".join(missing)
                          + (" ; tables non attendues : " + ", ".join(extra) if extra else ""))
    detail = f"{len(EXPECTED_TABLES)} tables attendues présentes"
    return detail + (" ; tables non attendues : " + ", ".join(extra) if extra else "")


def _check_postgis(fetch: Fetch, environment: str) -> str:
    rows = fetch(POSTGIS_SQL, {})
    if not rows:
        raise LookupError("extension postgis non installée")
    version, schema = rows[0]
    # Calling a PostGIS function proves the library is loadable, not only declared in the catalogue.
    library = fetch(f"SELECT {quote_identifier(schema)}.postgis_lib_version()", {})[0][0]
    return f"PostGIS {version} (bibliothèque {library}, schéma « {schema} »)"


CHECKS = (
    ("connexion PostgreSQL", _check_connection),
    (f"schéma {SCHEMA}", _check_schema),
    ("identité de l'environnement", _check_identity),
    (f"tables du schéma {SCHEMA}", _check_tables),
    ("PostGIS", _check_postgis),
)


def run_checks(fetch: Fetch, environment: str) -> tuple[Check, ...]:
    """Run every check in order; after the first failure the remaining ones are not executed."""
    results, failed = [], False
    for name, check in CHECKS:
        if failed:
            results.append(Check(name, SKIPPED, "contrôle précédent en échec"))
            continue
        try:
            results.append(Check(name, OK, check(fetch, environment)))
        except LookupError as error:
            results.append(Check(name, FAILED, str(error)))
            failed = True
    return tuple(results)


def diagnose(target: DatabaseTarget, engine=None) -> DiagnosticReport:
    """Diagnostic of ``target`` in one read-only transaction, rolled back at the end."""
    from sqlalchemy.exc import DBAPIError

    try:
        with _read_only_connection(target, engine) as connection:
            def fetch(sql: str, params: dict) -> Sequence[tuple]:
                # exec_driver_sql passes the psycopg-style %(name)s parameters straight to the driver.
                try:
                    return [tuple(row) for row in connection.exec_driver_sql(sql, params).all()]
                except DBAPIError as error:   # Permission denied, statement timeout...
                    reason = str(error.orig or error).strip().splitlines()[0]
                    raise LookupError(f"requête refusée : {type(error.orig or error).__name__} : {reason}") from None

            checks = run_checks(fetch, target.environment)
    except DatabaseConnectionError as error:
        checks = (Check(CHECKS[0][0], FAILED, str(error)),
                  *(Check(name, SKIPPED, "connexion impossible") for name, _ in CHECKS[1:]))
    return DiagnosticReport(target.describe(), checks)
