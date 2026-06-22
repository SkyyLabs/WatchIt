"""production household schema

Revision ID: 20260622_0002
Revises: 20260621_0001
Create Date: 2026-06-22
"""

from alembic import op

revision = "20260622_0002"
down_revision = "20260621_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    for old, new in (
        ("watchit_children", "children"),
        ("watchit_events", "events"),
        ("watchit_analysis", "analysis"),
        ("watchit_decisions", "decisions"),
        ("watchit_settings", "settings_legacy"),
        ("watchit_event_jobs", "event_jobs"),
        ("watchit_url_decision_cache", "url_decision_cache"),
    ):
        op.execute(
            f"""
            DO $$
            BEGIN
                IF to_regclass('public.{old}') IS NOT NULL AND to_regclass('public.{new}') IS NULL THEN
                    ALTER TABLE {old} RENAME TO {new};
                END IF;
            END $$;
            """
        )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS guardians(
            id TEXT PRIMARY KEY,
            clerk_user_id TEXT NOT NULL UNIQUE,
            email TEXT,
            display_name TEXT,
            created_at TIMESTAMPTZ DEFAULT now(),
            updated_at TIMESTAMPTZ DEFAULT now(),
            last_seen_at TIMESTAMPTZ
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS households(
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            created_at TIMESTAMPTZ DEFAULT now(),
            updated_at TIMESTAMPTZ DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS household_members(
            id TEXT PRIMARY KEY,
            household_id TEXT NOT NULL REFERENCES households(id) ON DELETE CASCADE,
            guardian_id TEXT NOT NULL REFERENCES guardians(id) ON DELETE CASCADE,
            role TEXT NOT NULL DEFAULT 'guardian',
            created_at TIMESTAMPTZ DEFAULT now(),
            UNIQUE(household_id, guardian_id)
        )
        """
    )

    op.execute("ALTER TABLE children ADD COLUMN IF NOT EXISTS household_id TEXT")
    op.execute("ALTER TABLE children ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'active'")
    op.execute("ALTER TABLE children ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT now()")
    op.execute(
        """
        INSERT INTO households(id, name)
        VALUES ('hh_legacy', 'Legacy Household')
        ON CONFLICT (id) DO NOTHING
        """
    )
    op.execute("UPDATE children SET household_id='hh_legacy' WHERE household_id IS NULL")
    op.execute("ALTER TABLE children ALTER COLUMN household_id SET NOT NULL")
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname='children_household_id_fkey'
            ) THEN
                ALTER TABLE children
                ADD CONSTRAINT children_household_id_fkey
                FOREIGN KEY (household_id) REFERENCES households(id) ON DELETE CASCADE;
            END IF;
        END $$;
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS devices(
            id TEXT PRIMARY KEY,
            household_id TEXT NOT NULL REFERENCES households(id) ON DELETE CASCADE,
            child_id TEXT NOT NULL REFERENCES children(id) ON DELETE CASCADE,
            device_name TEXT,
            device_type TEXT NOT NULL DEFAULT 'browser_extension',
            browser_name TEXT,
            browser_version TEXT,
            extension_version TEXT,
            install_id TEXT NOT NULL UNIQUE,
            token_hash TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL DEFAULT 'active',
            last_seen_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ DEFAULT now(),
            updated_at TIMESTAMPTZ DEFAULT now()
        )
        """
    )
    op.execute(
        """
        INSERT INTO devices(id, household_id, child_id, device_name, device_type, install_id, token_hash, status)
        SELECT 'dev_legacy', 'hh_legacy', id, 'Legacy browser extension', 'browser_extension', 'legacy-extension', 'legacy-token-disabled', 'revoked'
        FROM children
        WHERE id='child_main'
        ON CONFLICT (id) DO NOTHING
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS device_pairing_codes(
            id TEXT PRIMARY KEY,
            household_id TEXT NOT NULL REFERENCES households(id) ON DELETE CASCADE,
            child_id TEXT NOT NULL REFERENCES children(id) ON DELETE CASCADE,
            code_hash TEXT NOT NULL UNIQUE,
            expires_at TIMESTAMPTZ NOT NULL,
            redeemed_at TIMESTAMPTZ,
            created_by_guardian_id TEXT REFERENCES guardians(id) ON DELETE SET NULL,
            created_at TIMESTAMPTZ DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS monitoring_sessions(
            id TEXT PRIMARY KEY,
            household_id TEXT NOT NULL REFERENCES households(id) ON DELETE CASCADE,
            child_id TEXT NOT NULL REFERENCES children(id) ON DELETE CASCADE,
            device_id TEXT REFERENCES devices(id) ON DELETE SET NULL,
            started_by_guardian_id TEXT REFERENCES guardians(id) ON DELETE SET NULL,
            started_at TIMESTAMPTZ DEFAULT now(),
            stopped_by_guardian_id TEXT REFERENCES guardians(id) ON DELETE SET NULL,
            stopped_at TIMESTAMPTZ,
            stop_reason TEXT,
            status TEXT NOT NULL DEFAULT 'active'
        )
        """
    )

    op.execute("ALTER TABLE events ADD COLUMN IF NOT EXISTS household_id TEXT")
    op.execute("ALTER TABLE events ADD COLUMN IF NOT EXISTS device_id TEXT")
    op.execute("ALTER TABLE events ADD COLUMN IF NOT EXISTS session_id TEXT")
    op.execute("ALTER TABLE events ADD COLUMN IF NOT EXISTS normalized_url TEXT")
    op.execute("ALTER TABLE events ADD COLUMN IF NOT EXISTS domain TEXT")
    op.execute("ALTER TABLE events ADD COLUMN IF NOT EXISTS raw_json JSONB DEFAULT '{}'::jsonb")
    op.execute("ALTER TABLE events ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT now()")
    op.execute("UPDATE events SET household_id='hh_legacy' WHERE household_id IS NULL")
    op.execute("UPDATE events SET device_id='dev_legacy' WHERE device_id IS NULL AND to_regclass('public.devices') IS NOT NULL")
    op.execute("UPDATE events SET raw_json=data_json WHERE raw_json='{}'::jsonb AND data_json IS NOT NULL")
    op.execute("ALTER TABLE events ALTER COLUMN household_id SET NOT NULL")

    op.execute("ALTER TABLE event_jobs ADD COLUMN IF NOT EXISTS household_id TEXT")
    op.execute("UPDATE event_jobs SET household_id='hh_legacy' WHERE household_id IS NULL")
    op.execute("ALTER TABLE event_jobs ALTER COLUMN household_id SET NOT NULL")

    op.execute("ALTER TABLE analysis ADD COLUMN IF NOT EXISTS household_id TEXT")
    op.execute(
        """
        UPDATE analysis a
        SET household_id=e.household_id
        FROM events e
        WHERE a.event_id=e.id AND a.household_id IS NULL
        """
    )
    op.execute("UPDATE analysis SET household_id='hh_legacy' WHERE household_id IS NULL")
    op.execute("ALTER TABLE analysis ALTER COLUMN household_id SET NOT NULL")

    op.execute("ALTER TABLE decisions ADD COLUMN IF NOT EXISTS household_id TEXT")
    op.execute(
        """
        UPDATE decisions d
        SET household_id=e.household_id
        FROM events e
        WHERE d.event_id=e.id AND d.household_id IS NULL
        """
    )
    op.execute("UPDATE decisions SET household_id='hh_legacy' WHERE household_id IS NULL")
    op.execute("ALTER TABLE decisions ALTER COLUMN household_id SET NOT NULL")

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS decision_overrides(
            id TEXT PRIMARY KEY,
            household_id TEXT NOT NULL REFERENCES households(id) ON DELETE CASCADE,
            decision_id TEXT NOT NULL REFERENCES decisions(id) ON DELETE CASCADE,
            guardian_id TEXT REFERENCES guardians(id) ON DELETE SET NULL,
            action TEXT NOT NULL,
            reason TEXT,
            processed_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ DEFAULT now()
        )
        """
    )
    op.execute(
        """
        INSERT INTO decision_overrides(id, household_id, decision_id, action, processed_at, created_at)
        SELECT 'ovr_' || replace(id, 'dec_', ''), household_id, id, manual_action, NULL, to_timestamp(manual_updated_at / 1000.0)
        FROM decisions
        WHERE manual_flagged=TRUE AND manual_action IS NOT NULL
        ON CONFLICT (id) DO NOTHING
        """
    )

    op.execute("ALTER TABLE url_decision_cache ADD COLUMN IF NOT EXISTS household_id TEXT")
    op.execute("ALTER TABLE url_decision_cache ADD COLUMN IF NOT EXISTS domain TEXT")
    op.execute("ALTER TABLE url_decision_cache ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ")
    op.execute("UPDATE url_decision_cache SET household_id='hh_legacy' WHERE household_id IS NULL")
    op.execute("ALTER TABLE url_decision_cache ALTER COLUMN household_id SET NOT NULL")

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS child_schedules(
            id TEXT PRIMARY KEY,
            household_id TEXT NOT NULL REFERENCES households(id) ON DELETE CASCADE,
            child_id TEXT NOT NULL REFERENCES children(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            days TEXT NOT NULL,
            quiet_start TIME NOT NULL,
            quiet_end TIME NOT NULL,
            timezone TEXT,
            enabled BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMPTZ DEFAULT now(),
            updated_at TIMESTAMPTZ DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS household_settings(
            id TEXT PRIMARY KEY,
            household_id TEXT NOT NULL REFERENCES households(id) ON DELETE CASCADE,
            key TEXT NOT NULL,
            value_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            updated_by_guardian_id TEXT REFERENCES guardians(id) ON DELETE SET NULL,
            updated_at TIMESTAMPTZ DEFAULT now(),
            UNIQUE(household_id, key)
        )
        """
    )
    op.execute(
        """
        INSERT INTO household_settings(id, household_id, key, value_json)
        SELECT 'hset_' || md5('hh_legacy:' || key), 'hh_legacy', key, jsonb_build_object('value', value)
        FROM settings_legacy
        ON CONFLICT (household_id, key) DO NOTHING
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS guardian_feedback(
            id TEXT PRIMARY KEY,
            household_id TEXT NOT NULL REFERENCES households(id) ON DELETE CASCADE,
            guidance_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            sample_count INTEGER NOT NULL DEFAULT 0,
            generated_at BIGINT,
            created_at TIMESTAMPTZ DEFAULT now(),
            updated_at TIMESTAMPTZ DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS screenshot_files(
            id TEXT PRIMARY KEY,
            household_id TEXT NOT NULL REFERENCES households(id) ON DELETE CASCADE,
            child_id TEXT REFERENCES children(id) ON DELETE SET NULL,
            device_id TEXT REFERENCES devices(id) ON DELETE SET NULL,
            event_id TEXT REFERENCES events(id) ON DELETE CASCADE,
            session_id TEXT REFERENCES monitoring_sessions(id) ON DELETE SET NULL,
            local_path TEXT NOT NULL,
            sha256 TEXT,
            width INTEGER,
            height INTEGER,
            size_bytes BIGINT,
            ocr_text TEXT,
            captured_at TIMESTAMPTZ,
            retention_expires_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS audit_log(
            id TEXT PRIMARY KEY,
            household_id TEXT NOT NULL REFERENCES households(id) ON DELETE CASCADE,
            guardian_id TEXT REFERENCES guardians(id) ON DELETE SET NULL,
            device_id TEXT REFERENCES devices(id) ON DELETE SET NULL,
            action TEXT NOT NULL,
            entity_type TEXT,
            entity_id TEXT,
            metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS policy_versions(
            id TEXT PRIMARY KEY,
            version TEXT NOT NULL UNIQUE,
            description TEXT,
            config_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ DEFAULT now()
        )
        """
    )

    for table, column in (
        ("events", "household_id"),
        ("event_jobs", "household_id"),
        ("analysis", "household_id"),
        ("decisions", "household_id"),
        ("url_decision_cache", "household_id"),
    ):
        op.execute(
            f"""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint WHERE conname='{table}_{column}_fkey'
                ) THEN
                    ALTER TABLE {table}
                    ADD CONSTRAINT {table}_{column}_fkey
                    FOREIGN KEY ({column}) REFERENCES households(id) ON DELETE CASCADE;
                END IF;
            END $$;
            """
        )

    op.execute("CREATE INDEX IF NOT EXISTS idx_children_household ON children(household_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_devices_household_child ON devices(household_id, child_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_device_pairing_codes_hash ON device_pairing_codes(code_hash)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_monitoring_sessions_active ON monitoring_sessions(household_id, child_id, device_id, status)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_events_household_child_ts ON events(household_id, child_id, ts DESC)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_events_device_ts ON events(device_id, ts DESC)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_events_household_domain ON events(household_id, domain)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_event_jobs_status_created ON event_jobs(status, created_at)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_analysis_household_event ON analysis(household_id, event_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_decisions_household_event ON decisions(household_id, event_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_decision_overrides_household_decision ON decision_overrides(household_id, decision_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_url_decision_cache_scope_url ON url_decision_cache(household_id, child_id, normalized_url)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_url_decision_cache_expires ON url_decision_cache(expires_at)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_screenshot_files_event ON screenshot_files(event_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_audit_log_household_created ON audit_log(household_id, created_at DESC)")


def downgrade() -> None:
    raise NotImplementedError("Production schema migration is not safely reversible")
