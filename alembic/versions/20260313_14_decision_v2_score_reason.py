"""add decision v2 score and reason

Revision ID: 20260313_14
Revises: 20260312_13
Create Date: 2026-03-13 09:30:00.000000
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20260313_14"
down_revision = "20260312_13"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE tender_decisions ADD COLUMN IF NOT EXISTS decision_score INTEGER")
    op.execute("ALTER TABLE tender_decisions ADD COLUMN IF NOT EXISTS recommendation_reason TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE tender_decisions DROP COLUMN IF EXISTS recommendation_reason")
    op.execute("ALTER TABLE tender_decisions DROP COLUMN IF EXISTS decision_score")
