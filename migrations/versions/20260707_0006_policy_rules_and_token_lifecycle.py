"""guardian policy rules + device token lifecycle + policy freshness

Revision ID: 20260707_0006
Revises: 20260706_0005
Create Date: 2026-07-07
"""

from alembic import op

revision = "20260707_0006"
down_revision = "20260706_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Token lifecycle: expiry on issue, previous hash kept for a short grace
    # window after rotation so a lost rotation response can't brick a device.
    op.execute("ALTER TABLE devices ADD COLUMN IF NOT EXISTS token_expires_at TIMESTAMPTZ")
    op.execute("ALTER TABLE devices ADD COLUMN IF NOT EXISTS token_prev_hash TEXT")
    op.execute("ALTER TABLE devices ADD COLUMN IF NOT EXISTS token_rotated_at TIMESTAMPTZ")
    # Policy freshness: stamped whenever the device fetches its policy snapshot.
    op.execute("ALTER TABLE devices ADD COLUMN IF NOT EXISTS policy_fetched_at TIMESTAMPTZ")

    # Guardian-managed allow/block rules. child_id/device_id NULL widen the scope
    # (household-wide / all devices of the child); precedence is resolved in
    # watchit_core.policy.rules.
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS policy_rules(
            id TEXT PRIMARY KEY,
            household_id TEXT NOT NULL REFERENCES households(id) ON DELETE CASCADE,
            child_id TEXT REFERENCES children(id) ON DELETE CASCADE,
            device_id TEXT REFERENCES devices(id) ON DELETE CASCADE,
            action TEXT NOT NULL,
            rule_type TEXT NOT NULL,
            pattern TEXT NOT NULL,
            reason TEXT,
            created_by_guardian_id TEXT REFERENCES guardians(id) ON DELETE SET NULL,
            expires_at TIMESTAMPTZ,
            enabled BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMPTZ DEFAULT now(),
            updated_at TIMESTAMPTZ DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_policy_rules_scope "
        "ON policy_rules(household_id, child_id, device_id, enabled)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_policy_rules_scope")
    op.execute("DROP TABLE IF EXISTS policy_rules")
    op.execute("ALTER TABLE devices DROP COLUMN IF EXISTS policy_fetched_at")
    op.execute("ALTER TABLE devices DROP COLUMN IF EXISTS token_rotated_at")
    op.execute("ALTER TABLE devices DROP COLUMN IF EXISTS token_prev_hash")
    op.execute("ALTER TABLE devices DROP COLUMN IF EXISTS token_expires_at")
