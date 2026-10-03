"""add memory_upkeep_runs table

Revision ID: nn1a2b3c4d5e
Revises: mm1a2b3c4d5e
Create Date: 2026-10-03 00:00:00.000000

Adds the ``memory_upkeep_runs`` table backing Phase 3 of the long-term
memory reference implementation (``rollover/MEMORY-PLAN.md`` section 4).
One row per upkeep run; the per-user watermark is derived from the last
``succeeded`` run's ``window_until`` (no separate watermark table).

Brand-new table, created at the current schema state, so it carries the
tenant-partition ``workspace_id`` column as the leading primary-key member
(matching ``memory_claims`` and every other table after ``r1a2b3c4d5e6``).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision: str = "nn1a2b3c4d5e"
down_revision: str | None = "mm1a2b3c4d5e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the ``memory_upkeep_runs`` table."""
    op.create_table(
        "memory_upkeep_runs",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("id", Uuid16(), nullable=False),
        sa.Column("user_id", sa.String(128), nullable=False),
        sa.Column("window_since", sa.Integer(), nullable=False),
        sa.Column("window_until", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False, server_default="running"),
        sa.Column("disposition", sa.String(64), nullable=True),
        sa.Column("counts", sa.Text(), nullable=True),
        sa.Column("started_at", sa.Integer(), nullable=False),
        sa.Column("finished_at", sa.Integer(), nullable=True),
        sa.CheckConstraint(
            "state IN ('running', 'succeeded', 'failed', 'skipped')",
            name="ck_memory_upkeep_runs_state",
        ),
        sa.PrimaryKeyConstraint("workspace_id", "id"),
    )
    op.create_index(
        "ix_memory_upkeep_runs_user_scope",
        "memory_upkeep_runs",
        ["workspace_id", "user_id", "state", "started_at", "id"],
        unique=False,
    )


def downgrade() -> None:
    """Drop the ``memory_upkeep_runs`` table."""
    op.drop_index("ix_memory_upkeep_runs_user_scope", table_name="memory_upkeep_runs")
    op.drop_table("memory_upkeep_runs")
