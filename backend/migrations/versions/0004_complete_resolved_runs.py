"""weekly runs: mark completed the runs whose actions were all executed or rejected

From now on the gate completes a run when its last open action is decided; this catches up
the runs decided before that.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-19 23:00:00
"""
from collections.abc import Sequence

from alembic import op

revision: str = '0004'
down_revision: str | None = '0003'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE weekly_run SET status = 'completed'
        WHERE status = 'awaiting_approval'
          AND EXISTS (SELECT 1 FROM pending_action a
                      WHERE a.origin = 'weekly_run' AND a.origin_id = weekly_run.id)
          AND NOT EXISTS (SELECT 1 FROM pending_action a
                          WHERE a.origin = 'weekly_run' AND a.origin_id = weekly_run.id
                            AND a.status IN ('pending', 'approved', 'failed'))
        """
    )


def downgrade() -> None:
    op.execute("UPDATE weekly_run SET status = 'awaiting_approval' WHERE status = 'completed'")
