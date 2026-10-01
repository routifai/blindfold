"""add memory_claims table

Revision ID: mm1a2b3c4d5e
Revises: ll1a2b3c4d5e
Create Date: 2026-09-30 00:00:00.000000

Adds the ``memory_claims`` table backing the long-term memory reference
implementation (``rollover/MEMORY-PLAN.md``). One row per claim learned about
a user (preference, instruction, fact, decision, person, project, or working
style); the table is the source of truth and the txtai search index
(``omnigent/memory/index.py``) is rebuilt from it on demand.

Brand-new table, created at the current schema state, so it carries the
tenant-partition ``workspace_id`` column as the leading primary-key member
(matching every other table after ``r1a2b3c4d5e6``). No foreign-key
constraints (schema Rule R032 — see ``p1a2b3c4d5e6``): the
``supersedes_claim_id`` self-reference is enforced by the application.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision: str = "mm1a2b3c4d5e"
down_revision: str | None = "ll1a2b3c4d5e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the ``memory_claims`` table."""
    op.create_table(
        "memory_claims",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
        # UUID PK stored as 16 raw bytes (Uuid16 → BINARY(16) on MySQL, BLOB/BYTEA
        # elsewhere).
        sa.Column("id", Uuid16(), nullable=False),
        sa.Column("user_id", sa.String(128), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        # Opaque free text stored compressed (CompressedText → LargeBinary).
        sa.Column("claim_text", sa.LargeBinary(), nullable=False),
        sa.Column("quote", sa.LargeBinary(), nullable=True),
        sa.Column("speaker", sa.String(128), nullable=True),
        # JSON-encoded list of {"session_id", "item_id"} source links.
        sa.Column("evidence", sa.Text(), nullable=True),
        sa.Column("explicitness", sa.String(16), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("first_seen", sa.Integer(), nullable=False),
        sa.Column("reinforced_at", sa.Integer(), nullable=True),
        sa.Column("reinforcement_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("supersedes_claim_id", Uuid16(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("valid_until", sa.Integer(), nullable=True),
        sa.Column("run_id", sa.String(64), nullable=True),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.Integer(), nullable=True),
        sa.CheckConstraint(
            "kind IN ('preference', 'instruction', 'fact', 'decision', "
            "'person', 'project', 'working_style')",
            name="ck_memory_claims_kind",
        ),
        sa.CheckConstraint(
            "explicitness IN ('stated', 'inferred')",
            name="ck_memory_claims_explicitness",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'superseded', 'expired', 'forgotten')",
            name="ck_memory_claims_status",
        ),
        sa.PrimaryKeyConstraint("workspace_id", "id"),
    )
    op.create_index(
        "ix_memory_claims_user_scope",
        "memory_claims",
        ["workspace_id", "user_id", "status", "created_at", "id"],
        unique=False,
    )


def downgrade() -> None:
    """Drop the ``memory_claims`` table."""
    op.drop_index("ix_memory_claims_user_scope", table_name="memory_claims")
    op.drop_table("memory_claims")
