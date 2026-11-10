"""Read-only checks against a real Supabase/PostGIS project (marker ``postgis``); skipped by default.

Opt-in only: ``GEONAF_DB_INTEGRATION=1`` plus ``GEONAF_DATABASE_URL`` and ``GEONAF_DB_ENV`` (dev or
test), and requirements-db.txt. Every statement runs in a read-only transaction that is rolled
back: nothing is created, modified or deleted. See docs/database_connection.md.
"""

import os

import pytest

from src.data import database as db
from src.data import database_diagnostic as diag

ENABLED = os.environ.get("GEONAF_DB_INTEGRATION", "").strip() == "1"
pytestmark = [pytest.mark.postgis,
              pytest.mark.skipif(not ENABLED, reason="GEONAF_DB_INTEGRATION=1 non défini : base réelle non sollicitée.")]


@pytest.fixture
def target():
    pytest.importorskip("sqlalchemy", minversion="2.0")
    pytest.importorskip("psycopg", minversion="3.1")
    return db.load_target()


def test_diagnostic_is_conforming(target):
    report = diag.diagnose(target)
    assert report.ok, "\n".join(f"[{c.status}] {c.name} : {c.detail}" for c in report.checks)


def test_session_refuses_the_other_environment(target):
    other = "test" if target.environment == "dev" else "dev"
    with pytest.raises(db.EnvironmentIdentityError):
        with db.read_only_session(db.parse_target(target.url, other)):
            pytest.fail("La connexion ne doit pas être remise.")


def test_session_rejects_writes(target):
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    with pytest.raises(DBAPIError, match="read-only"):
        with db.read_only_session(target) as connection:
            # Even a temporary table is refused inside a READ ONLY transaction (and would vanish anyway).
            connection.execute(text("CREATE TEMPORARY TABLE geonaf_d21_probe (x integer)"))
