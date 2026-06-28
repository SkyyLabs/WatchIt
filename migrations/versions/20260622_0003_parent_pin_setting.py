"""db-managed parent pin setting

Revision ID: 20260622_0003
Revises: 20260622_0002
Create Date: 2026-06-22
"""

from alembic import op

revision = "20260622_0003"
down_revision = "20260622_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        DELETE FROM household_settings
        WHERE key='parent_pin'
          AND COALESCE(value_json->>'algorithm', '') <> 'pbkdf2_sha256'
        """
    )


def downgrade() -> None:
    pass
