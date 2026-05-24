"""profitability_status enum + snapshot input fields in tender_finance

Revision ID: 20260522_21
Revises: 20260522_20
Create Date: 2026-05-22 21:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260522_21"
down_revision: Union[str, None] = "20260522_20"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# ── Idempotency helpers ───────────────────────────────────────────────────────


def _column_exists(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = [c["name"] for c in inspector.get_columns(table_name)]
    return column_name in columns


def _column_type(table_name: str, column_name: str) -> str | None:
    """Return the udt_name (underlying data type) of a column, or None."""
    bind = op.get_bind()
    row = bind.execute(
        sa.text(
            "SELECT udt_name FROM information_schema.columns "
            "WHERE table_name = :t AND column_name = :c"
        ),
        {"t": table_name, "c": column_name},
    ).fetchone()
    return row[0] if row else None


# ── Upgrade ───────────────────────────────────────────────────────────────────


def upgrade() -> None:
    # 1. Create profitability_status_enum if it does not exist yet.
    #    Use DO…EXCEPTION so it is idempotent across repeated runs.
    op.execute(
        """
        DO $$ BEGIN
            CREATE TYPE profitability_status_enum AS ENUM
                ('go', 'no_go', 'requires_analysis');
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$
        """
    )
    # Commit so the new type is visible in the same session before ALTER.
    op.execute("COMMIT")

    # 2. Convert tender_finance.profitability_status from TEXT → ENUM.
    #    Skip if the column is already the enum type (udt_name = 'profitability_status_enum').
    col_type = _column_type("tender_finance", "profitability_status")
    if col_type == "text":
        op.execute(
            """
            ALTER TABLE tender_finance
                ALTER COLUMN profitability_status
                TYPE profitability_status_enum
                USING profitability_status::profitability_status_enum
            """
        )

    # 3. Add snapshot input columns (idempotent).
    if not _column_exists("tender_finance", "snapshot_contract_value"):
        op.add_column(
            "tender_finance",
            sa.Column("snapshot_contract_value", sa.Numeric(14, 2), nullable=True),
        )

    if not _column_exists("tender_finance", "snapshot_cost_estimate"):
        op.add_column(
            "tender_finance",
            sa.Column("snapshot_cost_estimate", sa.Numeric(14, 2), nullable=True),
        )

    if not _column_exists("tender_finance", "snapshot_participation_cost"):
        op.add_column(
            "tender_finance",
            sa.Column("snapshot_participation_cost", sa.Numeric(14, 2), nullable=True),
        )

    if not _column_exists("tender_finance", "snapshot_win_probability"):
        op.add_column(
            "tender_finance",
            sa.Column("snapshot_win_probability", sa.Numeric(5, 2), nullable=True),
        )


# ── Downgrade ─────────────────────────────────────────────────────────────────


def downgrade() -> None:
    for col in (
        "snapshot_win_probability",
        "snapshot_participation_cost",
        "snapshot_cost_estimate",
        "snapshot_contract_value",
    ):
        if _column_exists("tender_finance", col):
            op.drop_column("tender_finance", col)

    # Revert column back to TEXT (only if it is currently the enum type).
    col_type = _column_type("tender_finance", "profitability_status")
    if col_type == "profitability_status_enum":
        op.execute(
            "ALTER TABLE tender_finance "
            "ALTER COLUMN profitability_status TYPE TEXT "
            "USING profitability_status::TEXT"
        )

    op.execute(
        "DROP TYPE IF EXISTS profitability_status_enum"
    )
