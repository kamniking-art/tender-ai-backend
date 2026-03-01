"""add company profile jsonb and migrate data from ingestion settings

Revision ID: 20260301_10
Revises: 20260301_09
Create Date: 2026-03-01 12:40:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "20260301_10"
down_revision: Union[str, None] = "20260301_09"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "companies",
        sa.Column(
            "profile",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )

    op.execute(
        """
        UPDATE companies
        SET profile = ingestion_settings -> 'profile'
        WHERE (profile IS NULL OR profile = '{}'::jsonb)
          AND ingestion_settings ? 'profile'
          AND jsonb_typeof(ingestion_settings -> 'profile') = 'object'
        """
    )

    op.execute(
        """
        UPDATE companies
        SET ingestion_settings = ingestion_settings - 'profile'
        WHERE ingestion_settings ? 'profile'
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE companies
        SET ingestion_settings =
            CASE
                WHEN profile IS NOT NULL AND profile <> '{}'::jsonb
                    THEN jsonb_set(COALESCE(ingestion_settings, '{}'::jsonb), '{profile}', profile, true)
                ELSE COALESCE(ingestion_settings, '{}'::jsonb)
            END
        """
    )
    op.drop_column("companies", "profile")
