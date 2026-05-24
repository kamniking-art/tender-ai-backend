"""escalation fields: escalation_type, payload, telegram_message_id

Revision ID: 20260522_19
Revises: 20260520_18
Create Date: 2026-05-22 19:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260522_19"
down_revision: Union[str, None] = "20260520_18"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("SET LOCAL check_function_bodies = off")

    # Add 'reminded' first and commit before any statement references it.
    op.execute("ALTER TYPE escalation_status_enum ADD VALUE IF NOT EXISTS 'reminded'")
    op.execute("COMMIT")

    # Add new columns to escalations.
    op.add_column(
        "escalations",
        sa.Column("escalation_type", sa.Text(), nullable=True),
    )
    op.add_column(
        "escalations",
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column(
        "escalations",
        sa.Column("telegram_message_id", sa.Text(), nullable=True),
    )

    # Partial unique index: only one active escalation per
    # (company_id, tender_id, escalation_type).
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_escalation_active
        ON escalations (company_id, (payload->>'tender_id'), escalation_type)
        WHERE status IN ('pending', 'reminded')
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_escalation_active")
    op.drop_column("escalations", "telegram_message_id")
    op.drop_column("escalations", "payload")
    op.drop_column("escalations", "escalation_type")
    # Note: PostgreSQL does not support removing enum values — skip enum rollback.
