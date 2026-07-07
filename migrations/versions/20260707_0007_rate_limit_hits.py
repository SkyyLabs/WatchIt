"""shared-store rate limiting: hit log for sliding-window throttles

Revision ID: 20260707_0007
Revises: 20260707_0006
Create Date: 2026-07-07
"""

from alembic import op

revision = "20260707_0007"
down_revision = "20260707_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Ephemeral hit log backing the /v1/device/redeem throttle across API
    # instances. Rows live for one window; expired rows are pruned on each
    # check and by the retention sweep.
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS rate_limit_hits(
            scope TEXT NOT NULL,
            ts BIGINT NOT NULL
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_rate_limit_hits_scope_ts ON rate_limit_hits(scope, ts)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_rate_limit_hits_scope_ts")
    op.execute("DROP TABLE IF EXISTS rate_limit_hits")
