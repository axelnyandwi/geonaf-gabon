"""Run one SQL file of ``db/sql`` on the migration connection, sent to the driver as is.

The file goes to psycopg without parameters, so it is never parsed for bind markers and may hold
several statements and PL/pgSQL bodies.
"""

from pathlib import Path

SQL_DIR = Path(__file__).resolve().parents[1] / "sql"


def sql_text(name: str) -> str:
    return (SQL_DIR / name).read_text(encoding="utf-8")


def run_sql_file(op, name: str) -> None:
    driver_connection = op.get_bind().connection.driver_connection   # psycopg.Connection, same transaction.
    driver_connection.execute(sql_text(name))
