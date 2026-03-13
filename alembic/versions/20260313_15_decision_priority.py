"""add decision priority fields

Revision ID: 20260313_15
Revises: 20260313_14
Create Date: 2026-03-13 15:00:00.000000
"""

from alembic import op


# revision identifiers, used by Alembic.
revision = "20260313_15"
down_revision = "20260313_14"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE tender_decisions ADD COLUMN IF NOT EXISTS priority_score INTEGER")
    op.execute("ALTER TABLE tender_decisions ADD COLUMN IF NOT EXISTS priority_label TEXT")
    op.execute("ALTER TABLE tender_decisions ADD COLUMN IF NOT EXISTS priority_reason TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE tender_decisions DROP COLUMN IF EXISTS priority_reason")
    op.execute("ALTER TABLE tender_decisions DROP COLUMN IF EXISTS priority_label")
    op.execute("ALTER TABLE tender_decisions DROP COLUMN IF EXISTS priority_score")
