"""Guarded Alembic migrations; one atomic transaction, no implicit bootstrap or stamp."""
import hashlib
import os
import sys
from pathlib import Path

from alembic import context

# Also support the installed Alembic console script, whose sys.path starts in Scripts/.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.data.database import (
    DatabaseConfigError, check_identity, check_isolated_test_database, create_engine,
    isolated_test_identity, load_target,
)
from src.data.postgis_session import set_postgis_search_path

REVISIONS = ("0001_territorial_registry", "0002_observations")
TABLES_0001 = {"licences", "data_sources", "source_releases", "geometries", "territory_kinds",
               "legal_statuses", "territories", "registry_imports", "territory_versions"}
TABLES_0002 = {"data_imports", "acquisitions", "acquisition_manifests", "indicator_result_kinds",
               "indicator_definitions", "indicator_results"}
SQL_DIR = Path(__file__).resolve().parents[1] / "sql"


def refuse():
    raise DatabaseConfigError("DB_MIGRATION_STATE: état ou opération de migration non autorisé.")


def fingerprint(revision):
    return hashlib.sha256((SQL_DIR / (revision + ".up.sql")).read_text(encoding="utf-8").encode()).hexdigest()


def inspect_state(cursor, target, *, bootstrap=False):
    """Check identity, revision, table inventory and optional SQL Editor journal before writes."""
    cursor.execute("SELECT pg_catalog.to_regnamespace('geonaf'), "
                   "pg_catalog.obj_description(pg_catalog.to_regnamespace('geonaf'), 'pg_namespace'), "
                   "pg_catalog.to_regclass('public.alembic_version')")
    schema, mark, version_table = cursor.fetchone()
    heads = []
    if version_table is not None:
        cursor.execute("SELECT version_num FROM public.alembic_version")
        heads = [row[0] for row in cursor.fetchall()]
    if schema is None:
        if heads or not bootstrap:
            refuse()
        check_isolated_test_database(cursor, target)
        return (), False
    check_identity(mark, target.environment)
    if len(heads) != 1 or heads[0] not in REVISIONS:
        refuse()
    cursor.execute("SELECT tablename FROM pg_catalog.pg_tables WHERE schemaname = 'geonaf'")
    tables = {row[0] for row in cursor.fetchall()}
    journal = "schema_migrations" in tables
    expected = TABLES_0001 | (TABLES_0002 if heads[0] == REVISIONS[1] else set())
    if tables - {"schema_migrations"} != expected:
        refuse()
    if journal:
        cursor.execute("SELECT version, body_sha256 FROM geonaf.schema_migrations ORDER BY version")
        expected_history = [(r, fingerprint(r)) for r in REVISIONS[:REVISIONS.index(heads[0]) + 1]]
        if cursor.fetchall() != expected_history:
            refuse()
    return tuple(heads), journal


def run_migrations_online():
    engine = None
    try:
        target = load_target()
        bootstrap = os.environ.get("GEONAF_MIGRATION_BOOTSTRAP") == "1"
        if bootstrap:
            isolated_test_identity(target)  # Never connect remotely for bootstrap.
        engine = create_engine(target)
        with engine.connect() as connection:
            # Own the transaction BEFORE catalog reads (SQLAlchemy autobegin).
            # Alembic joins it: identity, SET LOCAL, DDL, history and version commit together.
            with connection.begin():
                driver = connection.connection.driver_connection
                with driver.cursor() as cursor:
                    heads, journal = inspect_state(cursor, target, bootstrap=bootstrap)
                    cursor.execute("SELECT pg_catalog.pg_advisory_xact_lock(714205, 1)")
                    heads, journal = inspect_state(cursor, target, bootstrap=bootstrap)
                    set_postgis_search_path(cursor)
                    context.configure(connection=connection, target_metadata=None,
                                      transaction_per_migration=False, transactional_ddl=True,
                                      version_table="alembic_version", version_table_schema="public")
                    migration = context.get_context()
                    if migration.opts.get("purge"):
                        refuse()
                    # Resolve the plan before Alembic can create/update its version table.
                    steps = list(migration.opts["fn"](heads, migration))
                    position = REVISIONS.index(heads[0]) + 1 if heads else 0
                    for step in steps:
                        info = step.info
                        if info.is_stamp or info.up_revision_id not in REVISIONS:
                            refuse()
                        expected_position = position if info.is_upgrade else position - 1
                        if not 0 <= expected_position < len(REVISIONS) or REVISIONS[expected_position] != info.up_revision_id:
                            refuse()
                        position += 1 if info.is_upgrade else -1
                        if not info.is_upgrade:
                            if journal:  # SQL Editor history is immutable; never rewrite it.
                                refuse()
                            check_isolated_test_database(cursor, target)

                    def after_revision(*, ctx, step, heads, run_args):
                        if step.is_upgrade and step.up_revision_id == REVISIONS[0]:
                            # 0001 just created the schema in this proven isolated transaction.
                            cursor.execute("COMMENT ON SCHEMA geonaf IS 'geonaf-environment:test'")
                        if journal:
                            cursor.execute("INSERT INTO geonaf.schema_migrations "
                                           "(version, body_sha256, channel) VALUES (%s, %s, %s)",
                                           (step.up_revision_id, fingerprint(step.up_revision_id), "alembic"))

                    context.configure(connection=connection, target_metadata=None,
                                      transaction_per_migration=False, transactional_ddl=True,
                                      version_table="alembic_version", version_table_schema="public",
                                      fn=lambda current_heads, ctx: iter(steps), on_version_apply=after_revision)
                    context.run_migrations()
                    final_heads, _ = inspect_state(cursor, target, bootstrap=bootstrap or not journal)
                    expected_heads = (REVISIONS[position - 1],) if position else ()
                    if final_heads != expected_heads or final_heads != tuple(context.get_context().get_current_heads()):
                        refuse()
    except Exception:
        # Driver details, SQL, server comments, URLs and local paths never reach CLI tracebacks.
        raise DatabaseConfigError("DB_MIGRATION_FAILED: migration refusée ou impossible ; "
                                  "vérifier la configuration, l'identité et l'historique.") from None
    finally:
        if engine is not None:
            try:
                engine.dispose()
            except Exception:
                raise DatabaseConfigError("DB_MIGRATION_CLOSE: fermeture du moteur impossible.") from None


if context.is_offline_mode():
    raise DatabaseConfigError("DB_MIGRATION_OFFLINE: mode hors ligne non autorisé.")
run_migrations_online()
