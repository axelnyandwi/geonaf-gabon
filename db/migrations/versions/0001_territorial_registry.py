"""Territorial registry: licences, sources, releases, geometries, territories and versions.

Revision ID: 0001_territorial_registry
Revises:
"""

import sys
from pathlib import Path

from alembic import op

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sql_files import run_sql_file  # noqa: E402

revision = "0001_territorial_registry"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    run_sql_file(op, "0001_territorial_registry.up.sql")


def downgrade() -> None:
    run_sql_file(op, "0001_territorial_registry.down.sql")
