"""add place_text to tenders

Revision ID: 20260312_13
Revises: 20260305_12
Create Date: 2026-03-12 18:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260312_13"
down_revision: str | None = "20260305_12"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("tenders", sa.Column("place_text", sa.Text(), nullable=True))
    op.create_index(op.f("ix_tenders_place_text"), "tenders", ["place_text"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_tenders_place_text"), table_name="tenders")
    op.drop_column("tenders", "place_text")
