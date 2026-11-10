"""Observations: satellite acquisitions, indicator definitions and results, import journal (D3).

Revision ID: 0002_observations
Revises: 0001_territorial_registry
"""

import sys
from pathlib import Path

from alembic import op

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sql_files import run_sql_file  # noqa: E402

revision = "0002_observations"
down_revision = "0001_territorial_registry"
branch_labels = None
depends_on = None


def upgrade() -> None:
    run_sql_file(op, "0002_observations.up.sql")


def downgrade() -> None:
    run_sql_file(op, "0002_observations.down.sql")
