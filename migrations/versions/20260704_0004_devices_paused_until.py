"""per-device timed pause

Revision ID: 20260704_0004
Revises: 20260622_0003
Create Date: 2026-07-04
"""

from alembic import op
import sqlalchemy as sa

revision = "20260704_0004"
down_revision = "20260622_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("devices", sa.Column("paused_until", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    op.drop_column("devices", "paused_until")
