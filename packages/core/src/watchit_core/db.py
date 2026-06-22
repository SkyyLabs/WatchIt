from __future__ import annotations

import json
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Json

from watchit_core.config import settings
from watchit_core.migrations import run_migrations


class Database:
    """Postgres-backed repository used by API and workers."""

    def __init__(self, dsn: Optional[str] = None):
        self.dsn = dsn

    def connect(self) -> None:
        run_migrations(self.dsn)

    def _connect(self):
        dsn = self.dsn or settings.database_url
        if not dsn:
            raise RuntimeError("DATABASE_URL is required")
        return psycopg.connect(dsn, row_factory=dict_row)

    def init_schema(self) -> None:
        """Legacy fallback for direct schema bootstrap. Prefer Alembic migrations."""
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS watchit_children(
                    id TEXT PRIMARY KEY,
                    name TEXT,
                    os_user TEXT,
                    timezone TEXT,
                    strictness TEXT DEFAULT 'standard',
                    age INTEGER DEFAULT 12,
                    created_at BIGINT DEFAULT (EXTRACT(EPOCH FROM now()) * 1000)::BIGINT
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS watchit_events(
                    id TEXT PRIMARY KEY,
                    child_id TEXT REFERENCES watchit_children(id),
                    ts BIGINT,
                    kind TEXT,
                    url TEXT,
                    title TEXT,
                    tab_id TEXT,
                    referrer TEXT,
                    data_json JSONB DEFAULT '{}'::jsonb
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS watchit_analysis(
                    id TEXT PRIMARY KEY,
                    event_id TEXT REFERENCES watchit_events(id) ON DELETE CASCADE,
                    model TEXT,
                    version TEXT,
                    scores_json JSONB DEFAULT '{}'::jsonb,
                    label TEXT,
                    latency_ms BIGINT
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS watchit_decisions(
                    id TEXT PRIMARY KEY,
                    event_id TEXT REFERENCES watchit_events(id) ON DELETE CASCADE,
                    policy_version TEXT,
                    action TEXT,
                    reason TEXT,
                    details_json JSONB DEFAULT '{}'::jsonb,
                    original_action TEXT,
                    manual_action TEXT,
                    manual_flagged BOOLEAN DEFAULT FALSE,
                    manual_processed BOOLEAN DEFAULT FALSE,
                    manual_updated_at BIGINT,
                    created_at TIMESTAMPTZ DEFAULT now()
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS watchit_settings(
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS watchit_event_jobs(
                    id TEXT PRIMARY KEY,
                    event_id TEXT,
                    event_json JSONB NOT NULL,
                    upgrade BOOLEAN DEFAULT FALSE,
                    status TEXT DEFAULT 'pending',
                    attempts INTEGER DEFAULT 0,
                    error TEXT,
                    created_at BIGINT,
                    claimed_at BIGINT,
                    completed_at BIGINT
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS watchit_url_decision_cache(
                    cache_key TEXT PRIMARY KEY,
                    normalized_url TEXT NOT NULL,
                    child_id TEXT,
                    strictness TEXT,
                    age INTEGER,
                    policy_version TEXT,
                    action TEXT NOT NULL,
                    reason TEXT,
                    details_json JSONB DEFAULT '{}'::jsonb,
                    source_decision_id TEXT,
                    source TEXT DEFAULT 'pipeline',
                    hit_count INTEGER DEFAULT 0,
                    created_at BIGINT,
                    updated_at BIGINT
                )
                """
            )
            cur.execute("CREATE INDEX IF NOT EXISTS idx_watchit_events_child_ts ON watchit_events(child_id, ts DESC)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_watchit_decisions_event ON watchit_decisions(event_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_watchit_jobs_status_created ON watchit_event_jobs(status, created_at)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_watchit_url_cache_url ON watchit_url_decision_cache(normalized_url)")

    def add_child_profile(self, child_id: str, name="", os_user="", timezone="", strictness: str = "standard", age: int = 12):
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO watchit_children(id, name, os_user, timezone, strictness, age)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO NOTHING
                """,
                (child_id, name, os_user, timezone, strictness, age),
            )

    def get_child_profile(self, child_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM watchit_children WHERE id=%s", (child_id,))
            return cur.fetchone()

    def update_child_profile(self, child_id: str, strictness: Optional[str] = None, age: Optional[int] = None):
        updates = []
        params: List[Any] = []
        if strictness is not None:
            updates.append("strictness=%s")
            params.append(strictness)
        if age is not None:
            updates.append("age=%s")
            params.append(age)
        if not updates:
            return
        params.append(child_id)
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(f"UPDATE watchit_children SET {', '.join(updates)} WHERE id=%s", params)

    def fetch_children(self) -> List[Dict[str, Any]]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT id, name, timezone, strictness, age, created_at FROM watchit_children ORDER BY created_at ASC")
            return cur.fetchall()

    def add_event(self, event: Dict[str, Any]) -> str:
        event_id = event.get("id") or f"evt_{uuid.uuid4().hex}"
        child_id = event.get("child_id", "child_default")
        self.add_child_profile(child_id)
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO watchit_events(id, child_id, ts, kind, url, title, tab_id, referrer, data_json)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    child_id=EXCLUDED.child_id,
                    ts=EXCLUDED.ts,
                    kind=EXCLUDED.kind,
                    url=EXCLUDED.url,
                    title=EXCLUDED.title,
                    tab_id=EXCLUDED.tab_id,
                    referrer=EXCLUDED.referrer,
                    data_json=EXCLUDED.data_json
                """,
                (
                    event_id,
                    child_id,
                    event.get("ts"),
                    event.get("kind"),
                    event.get("url"),
                    event.get("title"),
                    event.get("tab_id"),
                    event.get("referrer"),
                    Json(self._safe_json(event.get("data_json") or {})),
                ),
            )
        return event_id

    def enqueue_event_job(self, event: Dict[str, Any], upgrade: bool = False) -> Tuple[str, str]:
        event_id = event.get("id") or f"evt_{uuid.uuid4().hex}"
        event["id"] = event_id
        job_id = f"job_{uuid.uuid4().hex}"
        now_ms = int(time.time() * 1000)
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO watchit_event_jobs(id, event_id, event_json, upgrade, status, attempts, created_at)
                VALUES (%s, %s, %s, %s, 'pending', 0, %s)
                """,
                (job_id, event_id, Json(event), upgrade, now_ms),
            )
        return job_id, event_id

    def claim_event_jobs(self, limit: int = 5) -> List[Dict[str, Any]]:
        now_ms = int(time.time() * 1000)
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                WITH claimed AS (
                    SELECT id
                    FROM watchit_event_jobs
                    WHERE status='pending'
                    ORDER BY created_at ASC
                    LIMIT %s
                    FOR UPDATE SKIP LOCKED
                )
                UPDATE watchit_event_jobs j
                SET status='processing',
                    attempts=attempts + 1,
                    claimed_at=%s
                FROM claimed
                WHERE j.id=claimed.id
                RETURNING j.*
                """,
                (limit, now_ms),
            )
            return cur.fetchall()

    def complete_event_job(self, job_id: str) -> None:
        now_ms = int(time.time() * 1000)
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE watchit_event_jobs SET status='completed', completed_at=%s, error=NULL WHERE id=%s",
                (now_ms, job_id),
            )

    def fail_event_job(self, job_id: str, error: str) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE watchit_event_jobs SET status='failed', error=%s WHERE id=%s",
                ((error or "")[:1000], job_id),
            )

    def update_event_data_json(self, event_id: str, data_json: str):
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE watchit_events SET data_json=%s WHERE id=%s",
                (Json(self._safe_json(data_json or {})), event_id),
            )

    def add_analysis(self, event_id: str, model: str, version: str, scores: Dict[str, Any], label: str = "", latency_ms: Optional[int] = None) -> str:
        analysis_id = f"ana_{uuid.uuid4().hex}"
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO watchit_analysis(id, event_id, model, version, scores_json, label, latency_ms)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (analysis_id, event_id, model, version, Json(scores or {}), label, latency_ms),
            )
        return analysis_id

    def add_decision(self, event_id: str, policy_version: str, action: str, reason: str = "", details: Optional[Dict[str, Any]] = None) -> str:
        decision_id = f"dec_{uuid.uuid4().hex}"
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO watchit_decisions(id, event_id, policy_version, action, reason, details_json, original_action, manual_flagged, manual_processed)
                VALUES (%s, %s, %s, %s, %s, %s, %s, FALSE, FALSE)
                """,
                (decision_id, event_id, policy_version, action, reason, Json(details or {}), action),
            )
        return decision_id

    def get_cached_url_decision(self, cache_key: str, ttl_seconds: int) -> Optional[Dict[str, Any]]:
        min_updated_at = int(time.time() * 1000) - max(0, ttl_seconds) * 1000
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE watchit_url_decision_cache
                SET hit_count=hit_count + 1
                WHERE cache_key=%s AND updated_at >= %s
                RETURNING *
                """,
                (cache_key, min_updated_at),
            )
            return cur.fetchone()

    def upsert_url_decision_cache(
        self,
        *,
        cache_key: str,
        normalized_url: str,
        child_id: Optional[str],
        strictness: Optional[str],
        age: Optional[int],
        policy_version: str,
        action: str,
        reason: str,
        details: Optional[Dict[str, Any]],
        source_decision_id: Optional[str],
        source: str = "pipeline",
    ) -> None:
        now_ms = int(time.time() * 1000)
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO watchit_url_decision_cache(
                    cache_key,
                    normalized_url,
                    child_id,
                    strictness,
                    age,
                    policy_version,
                    action,
                    reason,
                    details_json,
                    source_decision_id,
                    source,
                    hit_count,
                    created_at,
                    updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 0, %s, %s)
                ON CONFLICT (cache_key) DO UPDATE SET
                    normalized_url=EXCLUDED.normalized_url,
                    child_id=EXCLUDED.child_id,
                    strictness=EXCLUDED.strictness,
                    age=EXCLUDED.age,
                    policy_version=EXCLUDED.policy_version,
                    action=EXCLUDED.action,
                    reason=EXCLUDED.reason,
                    details_json=EXCLUDED.details_json,
                    source_decision_id=EXCLUDED.source_decision_id,
                    source=EXCLUDED.source,
                    updated_at=EXCLUDED.updated_at
                """,
                (
                    cache_key,
                    normalized_url,
                    child_id,
                    strictness,
                    age,
                    policy_version,
                    action,
                    reason,
                    Json(details or {}),
                    source_decision_id,
                    source,
                    now_ms,
                    now_ms,
                ),
            )

    def get_recent_events(self, child_id: Optional[str], limit: int):
        with self._connect() as conn, conn.cursor() as cur:
            if child_id:
                cur.execute("SELECT * FROM watchit_events WHERE child_id=%s ORDER BY ts DESC LIMIT %s", (child_id, limit))
            else:
                cur.execute("SELECT * FROM watchit_events ORDER BY ts DESC LIMIT %s", (limit,))
            return cur.fetchall()

    def get_recent_decisions(self, child_id: Optional[str], limit: int):
        base_query = """
            SELECT
                d.id,
                d.event_id,
                d.policy_version,
                d.action,
                d.reason,
                d.details_json,
                d.original_action,
                d.manual_action,
                d.manual_flagged,
                d.manual_processed,
                d.manual_updated_at,
                e.tab_id,
                e.url,
                e.title,
                e.ts,
                e.child_id
            FROM watchit_decisions d
            JOIN watchit_events e ON d.event_id = e.id
        """
        with self._connect() as conn, conn.cursor() as cur:
            if child_id:
                cur.execute(base_query + " WHERE e.child_id=%s ORDER BY e.ts DESC LIMIT %s", (child_id, limit))
            else:
                cur.execute(base_query + " ORDER BY e.ts DESC LIMIT %s", (limit,))
            return cur.fetchall()

    def get_decision_with_event(self, decision_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT d.*, e.url, e.title, e.ts, e.child_id, e.tab_id
                FROM watchit_decisions d
                JOIN watchit_events e ON d.event_id=e.id
                WHERE d.id=%s
                """,
                (decision_id,),
            )
            return cur.fetchone()

    def override_decision(self, decision_id: str, new_action: str) -> Optional[Dict[str, Any]]:
        now_ms = int(time.time() * 1000)
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE watchit_decisions
                SET action=%s, manual_action=%s, manual_flagged=TRUE, manual_processed=FALSE, manual_updated_at=%s
                WHERE id=%s
                """,
                (new_action, new_action, now_ms, decision_id),
            )
            if cur.rowcount == 0:
                return None
        return self.get_decision_with_event(decision_id)

    def fetch_unprocessed_overrides(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT d.*, e.url, e.title, e.ts, e.child_id
                FROM watchit_decisions d
                JOIN watchit_events e ON d.event_id = e.id
                WHERE d.manual_flagged=TRUE AND d.manual_processed=FALSE
                ORDER BY d.manual_updated_at DESC
                LIMIT %s
                """,
                (limit,),
            )
            return cur.fetchall()

    def mark_override_processed(self, decision_ids: List[str]) -> None:
        if not decision_ids:
            return
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("UPDATE watchit_decisions SET manual_processed=TRUE WHERE id = ANY(%s)", (decision_ids,))

    def get_setting(self, key: str) -> Optional[str]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT value FROM watchit_settings WHERE key=%s", (key,))
            row = cur.fetchone()
            return row["value"] if row else None

    def set_setting(self, key: str, value: str) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO watchit_settings(key, value)
                VALUES (%s, %s)
                ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value
                """,
                (key, value),
            )

    def delete_setting(self, key: str) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM watchit_settings WHERE key=%s", (key,))

    def get_active_child_id(self) -> Optional[str]:
        return self.get_setting("active_child_id")

    def set_active_child_id(self, child_id: str) -> None:
        self.set_setting("active_child_id", child_id)

    def get_paused_until(self) -> Optional[int]:
        value = self.get_setting("paused_until")
        if not value:
            return None
        try:
            return int(value)
        except ValueError:
            return None

    @staticmethod
    def _safe_json(value: Any) -> Any:
        if isinstance(value, str):
            try:
                return json.loads(value) if value else {}
            except Exception:
                return {"raw": value}
        return value or {}


db = Database()
