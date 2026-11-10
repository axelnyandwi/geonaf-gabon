"""Destructive fixtures require a local capability and an independent database identity.

Never provisions a database or marks an existing one. An administrator must first
provision the database comment and give the test process its matching capability.
"""
from src.data.database import check_isolated_test_database, isolated_test_identity, parse_target
from src.data.postgis_session import connect_target


def isolated_connection(url, *, autocommit=False):
    target = parse_target(url, "test")
    isolated_test_identity(target)
    connection = connect_target(target)
    try:
        connection.read_only = True
        with connection.cursor() as cursor:
            check_isolated_test_database(cursor, target)
        connection.rollback()
        connection.read_only = False
        connection.autocommit = autocommit
        return connection
    except BaseException:
        connection.close()
        raise


def configure_alembic(monkeypatch, url):
    """Does not grant destructive access: the caller must already hold the capability."""
    isolated_test_identity(parse_target(url, "test"))
    monkeypatch.setenv("GEONAF_DATABASE_URL", url)
    monkeypatch.setenv("GEONAF_DB_ENV", "test")
    monkeypatch.setenv("GEONAF_MIGRATION_BOOTSTRAP", "1")
