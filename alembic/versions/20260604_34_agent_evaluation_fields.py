"""agent_evaluation: add was_right, notes, evaluated_at; migrate from old fields

Revision ID: 20260604_34
Revises: 20260529_33
Create Date: 2026-06-04 00:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260604_34"
down_revision: Union[str, None] = "20260529_33"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _column_exists(table: str, column: str) -> bool:
    from sqlalchemy import inspect
    bind = op.get_bind()
    return column in [c["name"] for c in inspect(bind).get_columns(table)]


def upgrade() -> None:
    # Add new columns if they don't exist yet
    if not _column_exists("agent_evaluation", "was_right"):
        op.add_column(
            "agent_evaluation",
            sa.Column("was_right", sa.Boolean(), nullable=True),
        )
        # Migrate data from old field if present
        if _column_exists("agent_evaluation", "was_agent_right"):
            op.execute(
                "UPDATE agent_evaluation SET was_right = was_agent_right"
            )

    if not _column_exists("agent_evaluation", "notes"):
        op.add_column(
            "agent_evaluation",
            sa.Column("notes", sa.Text(), nullable=True),
        )
        # Migrate data from old field if present
        if _column_exists("agent_evaluation", "reason_of_mismatch"):
            op.execute(
                "UPDATE agent_evaluation SET notes = reason_of_mismatch"
            )

    if not _column_exists("agent_evaluation", "evaluated_at"):
        op.add_column(
            "agent_evaluation",
            sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=True),
        )

    # Drop old columns if they exist
    if _column_exists("agent_evaluation", "was_agent_right"):
        op.drop_column("agent_evaluation", "was_agent_right")

    if _column_exists("agent_evaluation", "reason_of_mismatch"):
        op.drop_column("agent_evaluation", "reason_of_mismatch")


def downgrade() -> None:
    if not _column_exists("agent_evaluation", "was_agent_right"):
        op.add_column(
            "agent_evaluation",
            sa.Column("was_agent_right", sa.Boolean(), nullable=True),
        )
        if _column_exists("agent_evaluation", "was_right"):
            op.execute(
                "UPDATE agent_evaluation SET was_agent_right = was_right"
            )

    if not _column_exists("agent_evaluation", "reason_of_mismatch"):
        op.add_column(
            "agent_evaluation",
            sa.Column("reason_of_mismatch", sa.Text(), nullable=True),
        )
        if _column_exists("agent_evaluation", "notes"):
            op.execute(
                "UPDATE agent_evaluation SET reason_of_mismatch = notes"
            )

    if _column_exists("agent_evaluation", "was_right"):
        op.drop_column("agent_evaluation", "was_right")

    if _column_exists("agent_evaluation", "notes"):
        op.drop_column("agent_evaluation", "notes")

    if _column_exists("agent_evaluation", "evaluated_at"):
        op.drop_column("agent_evaluation", "evaluated_at")
