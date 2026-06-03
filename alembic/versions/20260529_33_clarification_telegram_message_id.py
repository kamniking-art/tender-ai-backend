"""add telegram_message_id to clarification_questions

Revision ID: 20260529_33
Revises: 20260529_32
Create Date: 2026-05-29 00:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260529_33"
down_revision: Union[str, None] = "20260529_32"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _column_exists(table: str, column: str) -> bool:
    from sqlalchemy import inspect
    bind = op.get_bind()
    return column in [c["name"] for c in inspect(bind).get_columns(table)]


def upgrade() -> None:
    if not _column_exists("clarification_questions", "telegram_message_id"):
        op.add_column(
            "clarification_questions",
            sa.Column("telegram_message_id", sa.Text(), nullable=True),
        )


def downgrade() -> None:
    if _column_exists("clarification_questions", "telegram_message_id"):
        op.drop_column("clarification_questions", "telegram_message_id")
