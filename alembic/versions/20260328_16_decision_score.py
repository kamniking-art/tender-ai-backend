"""add decision score field

Revision ID: 20260328_16
Revises: 20260313_15
Create Date: 2026-03-28 18:10:00.000000
"""

from alembic import op


# revision identifiers, used by Alembic.
revision = "20260328_16"
down_revision = "20260313_15"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE tender_decisions ADD COLUMN IF NOT EXISTS score INTEGER")


def downgrade() -> None:
    op.execute("ALTER TABLE tender_decisions DROP COLUMN IF EXISTS score")
