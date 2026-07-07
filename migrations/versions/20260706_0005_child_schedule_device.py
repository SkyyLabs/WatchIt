"""per-device quiet-hours override on child_schedules

Revision ID: 20260706_0005
Revises: 20260704_0004
Create Date: 2026-07-06
"""

from alembic import op

revision = "20260706_0005"
down_revision = "20260704_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # NULL device_id => child-level quiet hours (default for every device);
    # a set device_id => that device's override (device wins over child).
    op.execute(
        "ALTER TABLE child_schedules "
        "ADD COLUMN IF NOT EXISTS device_id TEXT REFERENCES devices(id) ON DELETE CASCADE"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_child_schedules_scope "
        "ON child_schedules(household_id, child_id, device_id, enabled)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_child_schedules_scope")
    op.execute("ALTER TABLE child_schedules DROP COLUMN IF EXISTS device_id")
