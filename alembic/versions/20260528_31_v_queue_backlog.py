"""create v_queue_backlog view

Revision ID: 20260528_31
Revises: 20260528_30
Create Date: 2026-05-28 00:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260528_31"
down_revision: Union[str, None] = "20260528_30"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_VIEW = "v_queue_backlog"

_SQL_UP = """
CREATE OR REPLACE VIEW v_queue_backlog AS
SELECT
    c.id                                                        AS company_id,
    c.name                                                      AS company_name,
    COUNT(t.id) FILTER (WHERE t.status = 'pending')             AS pending_tasks,
    COUNT(t.id) FILTER (WHERE t.status = 'overdue')             AS overdue_tasks,
    COUNT(a.action_id) FILTER (WHERE a.status = 'pending')       AS pending_actions,
    COUNT(a.action_id) FILTER (WHERE a.status = 'running')       AS running_actions
FROM companies c
LEFT JOIN tender_tasks t ON t.company_id = c.id
LEFT JOIN actions      a ON a.company_id = c.id
GROUP BY c.id, c.name
ORDER BY c.name;
"""

_SQL_DOWN = "DROP VIEW IF EXISTS v_queue_backlog;"


def upgrade() -> None:
    op.execute(sa.text(_SQL_UP))


def downgrade() -> None:
    op.execute(sa.text(_SQL_DOWN))
