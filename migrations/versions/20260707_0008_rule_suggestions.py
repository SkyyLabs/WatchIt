"""learned rules: suggestions distilled from repeated guardian overrides

Revision ID: 20260707_0008
Revises: 20260707_0007
Create Date: 2026-07-07
"""

from alembic import op

revision = "20260707_0008"
down_revision = "20260707_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Suggestions live apart from policy_rules: a rule row is guardian intent,
    # a suggestion is only a machine hypothesis until accepted.
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS rule_suggestions(
            id TEXT PRIMARY KEY,
            household_id TEXT NOT NULL REFERENCES households(id) ON DELETE CASCADE,
            child_id TEXT REFERENCES children(id) ON DELETE CASCADE,
            action TEXT NOT NULL,
            rule_type TEXT NOT NULL DEFAULT 'domain',
            pattern TEXT NOT NULL,
            evidence_count INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TIMESTAMPTZ DEFAULT now(),
            decided_at TIMESTAMPTZ,
            decided_by_guardian_id TEXT REFERENCES guardians(id) ON DELETE SET NULL
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_rule_suggestions_household_status ON rule_suggestions(household_id, status)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_rule_suggestions_household_status")
    op.execute("DROP TABLE IF EXISTS rule_suggestions")
