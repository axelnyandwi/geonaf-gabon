"""Alembic environment for GEONAF: explicit SQL migrations, no ORM metadata, online mode only.

The database URL comes from ``GEONAF_DATABASE_URL`` (``postgresql+psycopg://...``) and is never
printed: on error only the variable name is reported.
"""

import os

from alembic import context
from sqlalchemy import create_engine, pool

URL_VARIABLE = "GEONAF_DATABASE_URL"


def database_url() -> str:
    url = os.environ.get(URL_VARIABLE, "").strip()
    if not url:
        raise SystemExit(f"{URL_VARIABLE} n'est pas défini.")
    if not url.startswith("postgresql+psycopg://"):
        raise SystemExit(f"{URL_VARIABLE} doit utiliser le pilote psycopg (postgresql+psycopg://).")
    return url


def run_migrations_online() -> None:
    engine = create_engine(database_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=None, transaction_per_migration=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    raise SystemExit("Mode hors ligne non pris en charge : les migrations sont des fichiers SQL lisibles (db/sql).")
run_migrations_online()
