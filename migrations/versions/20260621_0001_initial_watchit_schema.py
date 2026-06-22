"""initial WatchIt schema

Revision ID: 20260621_0001
Revises:
Create Date: 2026-06-21
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20260621_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "watchit_children",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("name", sa.Text()),
        sa.Column("os_user", sa.Text()),
        sa.Column("timezone", sa.Text()),
        sa.Column("strictness", sa.Text(), server_default="standard"),
        sa.Column("age", sa.Integer(), server_default="12"),
        sa.Column("created_at", sa.BigInteger(), server_default=sa.text("(EXTRACT(EPOCH FROM now()) * 1000)::BIGINT")),
        if_not_exists=True,
    )
    op.create_table(
        "watchit_events",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("child_id", sa.Text(), sa.ForeignKey("watchit_children.id")),
        sa.Column("ts", sa.BigInteger()),
        sa.Column("kind", sa.Text()),
        sa.Column("url", sa.Text()),
        sa.Column("title", sa.Text()),
        sa.Column("tab_id", sa.Text()),
        sa.Column("referrer", sa.Text()),
        sa.Column("data_json", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb")),
        if_not_exists=True,
    )
    op.create_table(
        "watchit_analysis",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("event_id", sa.Text(), sa.ForeignKey("watchit_events.id", ondelete="CASCADE")),
        sa.Column("model", sa.Text()),
        sa.Column("version", sa.Text()),
        sa.Column("scores_json", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb")),
        sa.Column("label", sa.Text()),
        sa.Column("latency_ms", sa.BigInteger()),
        if_not_exists=True,
    )
    op.create_table(
        "watchit_decisions",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("event_id", sa.Text(), sa.ForeignKey("watchit_events.id", ondelete="CASCADE")),
        sa.Column("policy_version", sa.Text()),
        sa.Column("action", sa.Text()),
        sa.Column("reason", sa.Text()),
        sa.Column("details_json", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb")),
        sa.Column("original_action", sa.Text()),
        sa.Column("manual_action", sa.Text()),
        sa.Column("manual_flagged", sa.Boolean(), server_default=sa.text("FALSE")),
        sa.Column("manual_processed", sa.Boolean(), server_default=sa.text("FALSE")),
        sa.Column("manual_updated_at", sa.BigInteger()),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()")),
        if_not_exists=True,
    )
    op.create_table(
        "watchit_settings",
        sa.Column("key", sa.Text(), primary_key=True),
        sa.Column("value", sa.Text()),
        if_not_exists=True,
    )
    op.create_table(
        "watchit_event_jobs",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("event_id", sa.Text()),
        sa.Column("event_json", postgresql.JSONB(), nullable=False),
        sa.Column("upgrade", sa.Boolean(), server_default=sa.text("FALSE")),
        sa.Column("status", sa.Text(), server_default="pending"),
        sa.Column("attempts", sa.Integer(), server_default="0"),
        sa.Column("error", sa.Text()),
        sa.Column("created_at", sa.BigInteger()),
        sa.Column("claimed_at", sa.BigInteger()),
        sa.Column("completed_at", sa.BigInteger()),
        if_not_exists=True,
    )
    op.create_table(
        "watchit_url_decision_cache",
        sa.Column("cache_key", sa.Text(), primary_key=True),
        sa.Column("normalized_url", sa.Text(), nullable=False),
        sa.Column("child_id", sa.Text()),
        sa.Column("strictness", sa.Text()),
        sa.Column("age", sa.Integer()),
        sa.Column("policy_version", sa.Text()),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text()),
        sa.Column("details_json", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb")),
        sa.Column("source_decision_id", sa.Text()),
        sa.Column("source", sa.Text(), server_default="pipeline"),
        sa.Column("hit_count", sa.Integer(), server_default="0"),
        sa.Column("created_at", sa.BigInteger()),
        sa.Column("updated_at", sa.BigInteger()),
        if_not_exists=True,
    )

    op.create_index("idx_watchit_events_child_ts", "watchit_events", ["child_id", sa.text("ts DESC")], if_not_exists=True)
    op.create_index("idx_watchit_decisions_event", "watchit_decisions", ["event_id"], if_not_exists=True)
    op.create_index("idx_watchit_jobs_status_created", "watchit_event_jobs", ["status", "created_at"], if_not_exists=True)
    op.create_index("idx_watchit_url_cache_url", "watchit_url_decision_cache", ["normalized_url"], if_not_exists=True)


def downgrade() -> None:
    op.drop_index("idx_watchit_url_cache_url", table_name="watchit_url_decision_cache", if_exists=True)
    op.drop_index("idx_watchit_jobs_status_created", table_name="watchit_event_jobs", if_exists=True)
    op.drop_index("idx_watchit_decisions_event", table_name="watchit_decisions", if_exists=True)
    op.drop_index("idx_watchit_events_child_ts", table_name="watchit_events", if_exists=True)
    op.drop_table("watchit_url_decision_cache", if_exists=True)
    op.drop_table("watchit_event_jobs", if_exists=True)
    op.drop_table("watchit_settings", if_exists=True)
    op.drop_table("watchit_decisions", if_exists=True)
    op.drop_table("watchit_analysis", if_exists=True)
    op.drop_table("watchit_events", if_exists=True)
    op.drop_table("watchit_children", if_exists=True)
