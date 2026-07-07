from __future__ import annotations

import json
import hashlib
import hmac
import secrets
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

import threading

from psycopg.rows import dict_row
from psycopg.types.json import Json
from psycopg_pool import ConnectionPool

from watchit_core.config import settings
from watchit_core.migrations import run_migrations
from watchit_core.url_cache import normalize_url
from watchit_core.policy.rules import HIGH_RISK_TOKENS

# pg_notify channel carrying decision messages between worker and API instances
# when WATCHIT_SSE_BUS=postgres.
DECISION_CHANNEL = "watchit_decisions"


class Database:
    """Postgres-backed repository used by API and workers."""

    PARENT_PIN_KEY = "parent_pin"
    PARENT_PIN_ALGORITHM = "pbkdf2_sha256"
    PARENT_PIN_ITERATIONS = 260_000
    DEVICE_TOKEN_TTL_DAYS = 30
    # Grace window during which a rotated-out token still authenticates, so a
    # lost rotation response can't brick the device.
    TOKEN_ROTATION_GRACE_MINUTES = 60
    POLICY_SNAPSHOT_TTL_SECONDS = 86_400
    POLICY_SNAPSHOT_REFRESH_SECONDS = 900
    POLICY_SNAPSHOT_CACHE_LIMIT = 200
    # Retention windows (docs/PRIVACY_LOGGING_AND_RETENTION.md). Events carry
    # decisions/analysis via ON DELETE CASCADE, so they share one 12-month window.
    RETENTION_DOM_SAMPLE_DAYS = 7
    RETENTION_EVENT_JOBS_DAYS = 14
    RETENTION_SCREENSHOTS_DAYS = 30
    RETENTION_EVENTS_DAYS = 365
    RETENTION_AUDIT_DAYS = 730

    def __init__(self, dsn: Optional[str] = None):
        self.dsn = dsn
        self._pool: ConnectionPool | None = None
        self._pool_lock = threading.Lock()

    def connect(self) -> None:
        run_migrations(self.dsn)

    def _get_pool(self) -> ConnectionPool:
        if self._pool is None:
            with self._pool_lock:
                if self._pool is None:
                    dsn = self.dsn or settings.database_url
                    if not dsn:
                        raise RuntimeError("DATABASE_URL is required")
                    # Pooled connections avoid a fresh TCP+TLS handshake to Neon on
                    # every query. check_connection validates a conn before handing
                    # it out so idle Neon connections that dropped are recycled.
                    pool = ConnectionPool(
                        dsn,
                        min_size=1,
                        max_size=10,
                        kwargs={"row_factory": dict_row},
                        check=ConnectionPool.check_connection,
                        open=False,
                    )
                    pool.open()
                    self._pool = pool
        return self._pool

    def _connect(self):
        # Returns a context manager that yields a pooled connection and returns it
        # to the pool on exit (commit on success, rollback on error) — same
        # `with self._connect() as conn` call sites as before.
        return self._get_pool().connection()

    def ping(self) -> bool:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")
        return True

    def notify_decision(self, payload: str) -> None:
        # Shared SSE bus (WATCHIT_SSE_BUS=postgres): fan decision messages out to
        # every listening API instance via pg_notify. Payload is the serialized
        # decision message; callers keep it under the 8000-byte NOTIFY limit.
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT pg_notify(%s, %s)", (DECISION_CHANNEL, payload))

    def rate_limit_allow(self, scopes: List[Tuple[str, int]], window_seconds: int) -> bool:
        """Sliding-window throttle shared across API instances.

        `scopes` is [(scope_key, max_hits), ...]; the request is allowed only if
        every scope is under its limit, and then one hit is recorded per scope.
        Expired hits for the checked scopes are pruned in the same transaction.
        """
        now_ms = int(time.time() * 1000)
        cutoff_ms = now_ms - window_seconds * 1000
        scope_keys = [scope for scope, _ in scopes]
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM rate_limit_hits WHERE scope = ANY(%s) AND ts < %s", (scope_keys, cutoff_ms))
            cur.execute(
                "SELECT scope, COUNT(*) AS hits FROM rate_limit_hits WHERE scope = ANY(%s) GROUP BY scope",
                (scope_keys,),
            )
            counts = {row["scope"]: int(row["hits"]) for row in cur.fetchall()}
            if any(counts.get(scope, 0) >= limit for scope, limit in scopes):
                return False
            cur.executemany(
                "INSERT INTO rate_limit_hits(scope, ts) VALUES (%s, %s)",
                [(scope, now_ms) for scope in scope_keys],
            )
        return True

    def close(self) -> None:
        if self._pool is not None:
            self._pool.close()
            self._pool = None

    def init_schema(self) -> None:
        """Compatibility wrapper. Schema is owned by Alembic."""
        run_migrations(self.dsn)

    @staticmethod
    def _new_id(prefix: str) -> str:
        return f"{prefix}_{uuid.uuid4().hex}"

    @staticmethod
    def _token_hash(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    @staticmethod
    def _strip_screenshot_bytes(payload: Any) -> Any:
        # Invariant: image bytes never enter Postgres. The upgrade path carries
        # base64 screenshots inside data_json for the worker; keep them in the
        # transient queue payload only — the events row stores a count marker.
        if isinstance(payload, dict) and "screenshots_b64" in payload:
            payload = dict(payload)
            shots = payload.pop("screenshots_b64")
            payload["screenshot_count"] = len(shots) if isinstance(shots, list) else 1
        return payload

    @staticmethod
    def _domain(url: str | None) -> str:
        if not url:
            return ""
        try:
            host = urlsplit(url).netloc.lower()
        except Exception:
            return ""
        return host[4:] if host.startswith("www.") else host

    def ensure_guardian_household(self, claims: Dict[str, Any]) -> Dict[str, Any]:
        clerk_user_id = claims.get("sub")
        if not clerk_user_id:
            raise RuntimeError("Clerk token is missing subject")
        email = claims.get("email") or claims.get("primary_email_address")
        if not email and isinstance(claims.get("email_addresses"), list):
            email = (claims.get("email_addresses") or [{}])[0].get("email_address")
        display_name = claims.get("name") or claims.get("full_name") or email or clerk_user_id
        guardian_id = self._new_id("grd")
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO guardians(id, clerk_user_id, email, display_name, last_seen_at)
                VALUES (%s, %s, %s, %s, now())
                ON CONFLICT (clerk_user_id) DO UPDATE SET
                    email=COALESCE(EXCLUDED.email, guardians.email),
                    display_name=COALESCE(EXCLUDED.display_name, guardians.display_name),
                    last_seen_at=now(),
                    updated_at=now()
                RETURNING *
                """,
                (guardian_id, clerk_user_id, email, display_name),
            )
            guardian = cur.fetchone()
            cur.execute(
                """
                SELECT h.*, hm.role
                FROM households h
                JOIN household_members hm ON hm.household_id=h.id
                WHERE hm.guardian_id=%s
                ORDER BY hm.created_at ASC
                LIMIT 1
                """,
                (guardian["id"],),
            )
            household = cur.fetchone()
            if not household:
                cur.execute("SELECT id FROM household_members WHERE household_id='hh_legacy' LIMIT 1")
                legacy_claimed = cur.fetchone()
                household_id = "hh_legacy" if not legacy_claimed else self._new_id("hh")
                household_name = "Legacy Household" if household_id == "hh_legacy" else "My Household"
                cur.execute(
                    """
                    INSERT INTO households(id, name)
                    VALUES (%s, %s)
                    ON CONFLICT (id) DO UPDATE SET updated_at=now()
                    RETURNING *
                    """,
                    (household_id, household_name),
                )
                household = cur.fetchone()
                cur.execute(
                    """
                    INSERT INTO household_members(id, household_id, guardian_id, role)
                    VALUES (%s, %s, %s, 'owner')
                    ON CONFLICT (household_id, guardian_id) DO NOTHING
                    """,
                    (self._new_id("hm"), household_id, guardian["id"]),
                )
            return {"guardian": guardian, "household": household}

    def list_guardian_households(self, guardian_id: str) -> List[Dict[str, Any]]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT h.id, h.name, hm.role, hm.created_at AS joined_at
                FROM households h
                JOIN household_members hm ON hm.household_id=h.id
                WHERE hm.guardian_id=%s
                ORDER BY hm.created_at ASC
                """,
                (guardian_id,),
            )
            return cur.fetchall()

    def create_household(self, guardian_id: str, name: str) -> Dict[str, Any]:
        household_id = self._new_id("hh")
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO households(id, name) VALUES (%s, %s) RETURNING *",
                (household_id, name),
            )
            household = cur.fetchone()
            cur.execute(
                """
                INSERT INTO household_members(id, household_id, guardian_id, role)
                VALUES (%s, %s, %s, 'owner')
                ON CONFLICT (household_id, guardian_id) DO NOTHING
                """,
                (self._new_id("hm"), household_id, guardian_id),
            )
            return household

    def guardian_in_household(self, guardian_id: str, household_id: str) -> bool:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM household_members WHERE guardian_id=%s AND household_id=%s",
                (guardian_id, household_id),
            )
            return cur.fetchone() is not None

    def move_child(self, child_id: str, from_household_id: str, to_household_id: str) -> bool:
        # Reassign the child and everything the extension/monitoring path scopes by
        # household (devices carry the auth-derived household_id; sessions mirror it).
        # Historical events/decisions stay with the old household as an audit trail.
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE children SET household_id=%s, updated_at=now() WHERE id=%s AND household_id=%s",
                (to_household_id, child_id, from_household_id),
            )
            if cur.rowcount == 0:
                return False
            cur.execute(
                "UPDATE devices SET household_id=%s, updated_at=now() WHERE child_id=%s AND household_id=%s",
                (to_household_id, child_id, from_household_id),
            )
            cur.execute(
                "UPDATE monitoring_sessions SET household_id=%s WHERE child_id=%s AND household_id=%s",
                (to_household_id, child_id, from_household_id),
            )
            # Outstanding (unredeemed) pairing codes carry the source household_id;
            # redeem_pairing_code trusts it, so move them too or a pre-move code would
            # pair the child's browser back into the old household.
            cur.execute(
                """
                UPDATE device_pairing_codes SET household_id=%s
                WHERE child_id=%s AND household_id=%s AND redeemed_at IS NULL
                """,
                (to_household_id, child_id, from_household_id),
            )
            return True

    def log_audit(
        self,
        household_id: str,
        action: str,
        *,
        guardian_id: Optional[str] = None,
        device_id: Optional[str] = None,
        entity_type: Optional[str] = None,
        entity_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO audit_log(id, household_id, guardian_id, device_id, action, entity_type, entity_id, metadata_json)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (self._new_id("aud"), household_id, guardian_id, device_id, action, entity_type, entity_id, Json(metadata or {})),
            )

    def add_child_profile(
        self,
        child_id: str,
        name: str = "",
        os_user: str = "",
        timezone: str = "",
        strictness: str = "standard",
        age: int = 12,
        household_id: str = "hh_legacy",
    ):
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO children(id, household_id, name, timezone, strictness, age, status)
                VALUES (%s, %s, %s, %s, %s, %s, 'active')
                ON CONFLICT (id) DO NOTHING
                """,
                (child_id, household_id, name or os_user or "", timezone, strictness, age),
            )

    def get_child_profile(self, child_id: str, household_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        with self._connect() as conn, conn.cursor() as cur:
            if household_id:
                cur.execute("SELECT * FROM children WHERE id=%s AND household_id=%s", (child_id, household_id))
            else:
                cur.execute("SELECT * FROM children WHERE id=%s", (child_id,))
            return cur.fetchone()

    def update_child_profile(
        self,
        child_id: str,
        strictness: Optional[str] = None,
        age: Optional[int] = None,
        household_id: Optional[str] = None,
        name: Optional[str] = None,
        timezone: Optional[str] = None,
    ):
        updates = []
        params: List[Any] = []
        if strictness is not None:
            updates.append("strictness=%s")
            params.append(strictness)
        if age is not None:
            updates.append("age=%s")
            params.append(age)
        if name is not None:
            updates.append("name=%s")
            params.append(name)
        if timezone is not None:
            updates.append("timezone=%s")
            params.append(timezone)
        if not updates:
            return
        updates.append("updated_at=now()")
        params.append(child_id)
        where = "id=%s"
        if household_id:
            params.append(household_id)
            where += " AND household_id=%s"
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(f"UPDATE children SET {', '.join(updates)} WHERE {where}", params)

    def fetch_children(self, household_id: Optional[str] = None) -> List[Dict[str, Any]]:
        # monitoring_active / active_device_count let the children UI reflect live
        # state on load without an extra fetch per child.
        columns = """
            c.id, c.household_id, c.name, c.timezone, c.strictness, c.age, c.status,
            c.created_at, c.updated_at,
            EXISTS(
                SELECT 1 FROM monitoring_sessions ms
                WHERE ms.household_id=c.household_id AND ms.child_id=c.id AND ms.status='active'
            ) AS monitoring_active,
            (
                SELECT COUNT(*) FROM devices d
                WHERE d.household_id=c.household_id AND d.child_id=c.id AND d.status='active'
            ) AS active_device_count
        """
        with self._connect() as conn, conn.cursor() as cur:
            if household_id:
                cur.execute(
                    f"SELECT {columns} FROM children c WHERE c.household_id=%s ORDER BY c.created_at ASC",
                    (household_id,),
                )
            else:
                cur.execute(f"SELECT {columns} FROM children c ORDER BY c.created_at ASC")
            return cur.fetchall()

    def fetch_guardian_children(self, guardian_id: str) -> List[Dict[str, Any]]:
        # Children across every household the guardian belongs to, tagged with the
        # household name — powers the household view's move-in / move-out lists.
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT c.id, c.household_id, h.name AS household_name,
                       c.name, c.strictness, c.age, c.status
                FROM children c
                JOIN household_members hm ON hm.household_id=c.household_id
                JOIN households h ON h.id=c.household_id
                WHERE hm.guardian_id=%s
                ORDER BY h.name ASC, c.created_at ASC
                """,
                (guardian_id,),
            )
            return cur.fetchall()

    # --- Quiet-hours schedules -------------------------------------------------
    # quiet_start/quiet_end are TIME columns; read them back as "HH:MM" strings so
    # the API and the worker policy gate don't juggle datetime.time objects.
    _SCHEDULE_COLUMNS = (
        "id, household_id, child_id, device_id, name, days, "
        "to_char(quiet_start, 'HH24:MI') AS quiet_start, "
        "to_char(quiet_end, 'HH24:MI') AS quiet_end, timezone, enabled"
    )

    def list_schedules(self, household_id: str, child_id: str) -> List[Dict[str, Any]]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT {self._SCHEDULE_COLUMNS} FROM child_schedules "
                "WHERE household_id=%s AND child_id=%s "
                "ORDER BY device_id NULLS FIRST, created_at ASC",
                (household_id, child_id),
            )
            return cur.fetchall()

    def upsert_schedule(
        self,
        household_id: str,
        child_id: str,
        days: str,
        quiet_start: str,
        quiet_end: str,
        *,
        device_id: Optional[str] = None,
        name: str = "Quiet hours",
        enabled: bool = True,
        timezone: Optional[str] = None,
        schedule_id: Optional[str] = None,
    ) -> str:
        with self._connect() as conn, conn.cursor() as cur:
            if schedule_id:
                cur.execute(
                    """
                    UPDATE child_schedules
                    SET device_id=%s, name=%s, days=%s, quiet_start=%s, quiet_end=%s,
                        timezone=%s, enabled=%s, updated_at=now()
                    WHERE id=%s AND household_id=%s
                    RETURNING id
                    """,
                    (device_id, name, days, quiet_start, quiet_end, timezone, enabled, schedule_id, household_id),
                )
                row = cur.fetchone()
                if row:
                    return row["id"]
            new_id = self._new_id("sch")
            cur.execute(
                """
                INSERT INTO child_schedules(id, household_id, child_id, device_id, name, days, quiet_start, quiet_end, timezone, enabled)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (new_id, household_id, child_id, device_id, name, days, quiet_start, quiet_end, timezone, enabled),
            )
            return cur.fetchone()["id"]

    def delete_schedule(self, schedule_id: str, household_id: str) -> int:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "DELETE FROM child_schedules WHERE id=%s AND household_id=%s",
                (schedule_id, household_id),
            )
            return cur.rowcount

    def get_effective_quiet_schedules(
        self, household_id: str, child_id: str, device_id: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        # Return every enabled window that applies, so the caller can match any of
        # them (a child may have e.g. separate weekday/weekend windows). Device-level
        # windows override the child defaults entirely when present. timezone falls
        # back to the child profile's timezone so windows evaluate in local time.
        cols = (
            "cs.id, cs.household_id, cs.child_id, cs.device_id, cs.name, cs.days, "
            "to_char(cs.quiet_start, 'HH24:MI') AS quiet_start, "
            "to_char(cs.quiet_end, 'HH24:MI') AS quiet_end, "
            "COALESCE(NULLIF(cs.timezone, ''), NULLIF(c.timezone, '')) AS timezone, cs.enabled"
        )
        base = (
            f"SELECT {cols} FROM child_schedules cs JOIN children c ON c.id=cs.child_id "
            "WHERE cs.household_id=%s AND cs.child_id=%s AND cs.enabled=TRUE"
        )
        with self._connect() as conn, conn.cursor() as cur:
            if device_id:
                cur.execute(
                    base + " AND cs.device_id=%s ORDER BY cs.created_at ASC",
                    (household_id, child_id, device_id),
                )
                rows = cur.fetchall()
                if rows:
                    return rows
            cur.execute(
                base + " AND cs.device_id IS NULL ORDER BY cs.created_at ASC",
                (household_id, child_id),
            )
            return cur.fetchall()

    def fetch_devices(self, household_id: str, child_id: str) -> List[Dict[str, Any]]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, child_id, device_name, browser_name, status,
                       last_seen_at, policy_fetched_at, paused_until, created_at
                FROM devices
                WHERE household_id=%s AND child_id=%s AND status<>'revoked'
                ORDER BY created_at DESC
                """,
                (household_id, child_id),
            )
            return cur.fetchall()

    def fetch_household_devices(self, household_id: str) -> List[Dict[str, Any]]:
        # Household-wide device list (all children) for the dashboard's
        # protection-status view; one query instead of per-child fetches.
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT d.id, d.child_id, c.name AS child_name, d.device_name, d.browser_name,
                       d.status, d.last_seen_at, d.policy_fetched_at, d.paused_until, d.created_at
                FROM devices d
                JOIN children c ON c.id=d.child_id
                WHERE d.household_id=%s AND d.status<>'revoked'
                ORDER BY d.created_at DESC
                """,
                (household_id,),
            )
            return cur.fetchall()

    # --- Guardian manual allow/block rules --------------------------------------

    def create_rule(
        self,
        household_id: str,
        *,
        action: str,
        rule_type: str,
        pattern: str,
        child_id: Optional[str] = None,
        device_id: Optional[str] = None,
        reason: Optional[str] = None,
        expires_at: Optional[datetime] = None,
        created_by_guardian_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO policy_rules(
                    id, household_id, child_id, device_id, action, rule_type, pattern,
                    reason, created_by_guardian_id, expires_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING *
                """,
                (
                    self._new_id("rule"),
                    household_id,
                    child_id,
                    device_id,
                    action,
                    rule_type,
                    pattern,
                    reason,
                    created_by_guardian_id,
                    expires_at,
                ),
            )
            return cur.fetchone()

    def list_rules(self, household_id: str, child_id: Optional[str] = None) -> List[Dict[str, Any]]:
        # Guardian view: household-wide rules plus (optionally) one child's rules.
        with self._connect() as conn, conn.cursor() as cur:
            params: List[Any] = [household_id]
            scope = ""
            if child_id:
                params.append(child_id)
                scope = " AND (child_id IS NULL OR child_id=%s)"
            cur.execute(
                f"""
                SELECT * FROM policy_rules
                WHERE household_id=%s AND enabled=TRUE
                {scope}
                ORDER BY created_at DESC
                """,
                params,
            )
            return cur.fetchall()

    def delete_rule(self, rule_id: str, household_id: str) -> int:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "DELETE FROM policy_rules WHERE id=%s AND household_id=%s",
                (rule_id, household_id),
            )
            return cur.rowcount

    SUGGESTION_MIN_OVERRIDES = 3
    SUGGESTION_WINDOW_DAYS = 30

    def generate_rule_suggestions(self) -> int:
        """Distill repeated same-direction overrides into pending rule
        suggestions (learned rules with guardian review — never auto-applied).

        A candidate is (household, child, domain, action) with >= 3 overrides in
        30 days, no enabled rule already covering the domain, no pending
        suggestion, and no dismissal within the window (a guardian's "no" is
        respected, not re-asked weekly). Platform-wide by design, like the
        retention sweep — the learning loop is not a per-tenant request path.
        """
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                WITH candidates AS (
                    SELECT o.household_id, e.child_id, e.domain AS pattern, o.action, COUNT(*) AS n
                    FROM decision_overrides o
                    JOIN decisions d ON o.decision_id = d.id
                    JOIN events e ON d.event_id = e.id
                    WHERE o.created_at > now() - make_interval(days => %s)
                      AND o.action IN ('allow', 'block')
                      AND COALESCE(e.domain, '') <> ''
                    GROUP BY o.household_id, e.child_id, e.domain, o.action
                    HAVING COUNT(*) >= %s
                )
                INSERT INTO rule_suggestions(id, household_id, child_id, action, rule_type, pattern, evidence_count)
                SELECT 'rsug_' || replace(gen_random_uuid()::text, '-', ''),
                       c.household_id, c.child_id, c.action, 'domain', c.pattern, c.n
                FROM candidates c
                WHERE NOT EXISTS (
                    SELECT 1 FROM policy_rules r
                    WHERE r.household_id = c.household_id AND r.enabled = TRUE AND r.pattern = c.pattern
                      AND (r.child_id IS NULL OR r.child_id = c.child_id)
                )
                AND NOT EXISTS (
                    SELECT 1 FROM rule_suggestions s
                    WHERE s.household_id = c.household_id
                      AND COALESCE(s.child_id, '') = COALESCE(c.child_id, '')
                      AND s.pattern = c.pattern AND s.action = c.action
                      AND (s.status = 'pending'
                           OR (s.status = 'dismissed' AND s.decided_at > now() - make_interval(days => %s)))
                )
                """,
                (self.SUGGESTION_WINDOW_DAYS, self.SUGGESTION_MIN_OVERRIDES, self.SUGGESTION_WINDOW_DAYS),
            )
            return cur.rowcount

    def list_rule_suggestions(self, household_id: str) -> List[Dict[str, Any]]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM rule_suggestions
                WHERE household_id=%s AND status='pending'
                ORDER BY evidence_count DESC, created_at DESC
                """,
                (household_id,),
            )
            return cur.fetchall()

    def resolve_rule_suggestion(
        self, suggestion_id: str, household_id: str, *, status: str, guardian_id: Optional[str]
    ) -> Optional[Dict[str, Any]]:
        # accepted/dismissed; returns the suggestion row or None when it isn't
        # this household's pending suggestion.
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE rule_suggestions
                SET status=%s, decided_at=now(), decided_by_guardian_id=%s
                WHERE id=%s AND household_id=%s AND status='pending'
                RETURNING *
                """,
                (status, guardian_id, suggestion_id, household_id),
            )
            return cur.fetchone()

    def list_active_rules(
        self, household_id: str, child_id: Optional[str], device_id: Optional[str]
    ) -> List[Dict[str, Any]]:
        """Enforcement view: every enabled, unexpired rule that applies to this
        child+device (household-wide rules included). Precedence is resolved by
        watchit_core.policy.rules.evaluate_rules."""
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, household_id, child_id, device_id, action, rule_type,
                       pattern, reason, expires_at, enabled
                FROM policy_rules
                WHERE household_id=%s AND enabled=TRUE
                  AND (child_id IS NULL OR child_id=%s)
                  AND (device_id IS NULL OR device_id=%s)
                  AND (expires_at IS NULL OR expires_at > now())
                ORDER BY created_at ASC
                """,
                (household_id, child_id, device_id),
            )
            return cur.fetchall()

    # --- Device policy snapshot --------------------------------------------------

    @staticmethod
    def _to_epoch_ms(value: Any) -> Optional[int]:
        if value is None:
            return None
        if isinstance(value, (int, float)):
            return int(value)
        if isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            return int(value.timestamp() * 1000)
        return None

    def list_snapshot_cached_decisions(self, household_id: str, child_id: Optional[str]) -> List[Dict[str, Any]]:
        min_updated_at = int(time.time() * 1000) - settings.url_decision_cache_ttl_seconds * 1000
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT normalized_url, action, reason, details_json, source
                FROM url_decision_cache
                WHERE household_id=%s AND child_id=%s AND updated_at >= %s
                ORDER BY updated_at DESC
                LIMIT %s
                """,
                (household_id, child_id, min_updated_at, self.POLICY_SNAPSHOT_CACHE_LIMIT),
            )
            rows = cur.fetchall()
        # Only high-confidence entries are safe to enforce locally.
        out = []
        for row in rows:
            details = row.get("details_json") or {}
            confidence = float(details.get("confidence", 0.0) or 0.0)
            if row.get("source") == "manual_override" or confidence >= settings.url_decision_cache_min_confidence:
                out.append({
                    "normalized_url": row["normalized_url"],
                    "action": row["action"],
                    "reason": row.get("reason") or "",
                })
        return out

    def stamp_policy_fetch(self, device_id: str) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("UPDATE devices SET policy_fetched_at=now() WHERE id=%s", (device_id,))

    def build_policy_snapshot(self, device: Dict[str, Any]) -> Dict[str, Any]:
        """Versioned, self-contained policy view for one device. The extension
        stores it locally and enforces layers 1-4 without a network round trip."""
        household_id = device["household_id"]
        child_id = device.get("child_id")
        device_id = device["id"]
        profile = self.get_child_profile(child_id, household_id) if child_id else None

        rules = []
        for rule in self.list_active_rules(household_id, child_id, device_id):
            scope = "device" if rule.get("device_id") else ("child" if rule.get("child_id") else "household")
            rules.append({
                "id": rule["id"],
                "action": rule["action"],
                "rule_type": rule["rule_type"],
                "pattern": rule["pattern"],
                "scope": scope,
                "child_id": rule.get("child_id"),
                "device_id": rule.get("device_id"),
                "expires_at": self._to_epoch_ms(rule.get("expires_at")),
            })

        quiet_hours = [
            {
                "days": s.get("days") or "",
                "quiet_start": s.get("quiet_start"),
                "quiet_end": s.get("quiet_end"),
                "timezone": s.get("timezone"),
                "device_id": s.get("device_id"),
            }
            for s in (self.get_effective_quiet_schedules(household_id, child_id, device_id) if child_id else [])
        ]

        household_pause = self.get_paused_until(household_id)
        device_pause = device.get("paused_until")
        pauses = [p for p in (household_pause, device_pause) if p]

        content = {
            "household_id": household_id,
            "child_id": child_id,
            "device_id": device_id,
            "policy_version": settings.policy_version,
            "device_status": device.get("status"),
            "monitoring_enabled": self.is_household_monitoring_enabled(household_id),
            "monitoring_active": bool(
                child_id and self.get_active_monitoring_session(household_id, child_id, device_id)
            ),
            "paused_until": max(pauses) if pauses else None,
            "strictness": (profile or {}).get("strictness") or "standard",
            "rules": rules,
            "quiet_hours": quiet_hours,
            "cached_decisions": self.list_snapshot_cached_decisions(household_id, child_id),
            "high_risk_tokens": HIGH_RISK_TOKENS,
        }
        version = hashlib.sha256(
            json.dumps(content, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()[:16]
        now_ms = int(time.time() * 1000)
        return {
            **content,
            "version": version,
            "issued_at": now_ms,
            "expires_at": now_ms + self.POLICY_SNAPSHOT_TTL_SECONDS * 1000,
            "refresh_after_seconds": self.POLICY_SNAPSHOT_REFRESH_SECONDS,
            "token_expires_at": self._to_epoch_ms(device.get("token_expires_at")),
        }

    def get_device_paused_until(self, device_id: str) -> Optional[int]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT paused_until FROM devices WHERE id=%s", (device_id,))
            row = cur.fetchone()
            return row["paused_until"] if row else None

    def set_device_pause(self, household_id: str, device_id: str, paused_until_ms: int) -> int:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE devices SET paused_until=%s, updated_at=now() WHERE id=%s AND household_id=%s",
                (paused_until_ms, device_id, household_id),
            )
            return cur.rowcount

    def clear_device_pause(self, household_id: str, device_id: str) -> int:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE devices SET paused_until=NULL, updated_at=now() WHERE id=%s AND household_id=%s",
                (device_id, household_id),
            )
            return cur.rowcount

    def revoke_device(self, household_id: str, device_id: str) -> int:
        # Revoking retires a stale/duplicate device: it drops out of the list and its
        # token stops authenticating (authenticate_device_token requires status='active').
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE devices SET status='revoked', updated_at=now() WHERE id=%s AND household_id=%s AND status<>'revoked'",
                (device_id, household_id),
            )
            return cur.rowcount

    def add_event(self, event: Dict[str, Any]) -> str:
        event_id = event.get("id") or f"evt_{uuid.uuid4().hex}"
        child_id = event.get("child_id", "child_default")
        household_id = event.get("household_id") or "hh_legacy"
        self.add_child_profile(child_id, household_id=household_id)
        normalized = normalize_url(event.get("url"))
        domain = self._domain(normalized or event.get("url"))
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO events(
                    id, household_id, child_id, device_id, session_id, ts, kind, url, normalized_url, domain, title, tab_id, referrer, data_json, raw_json
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    household_id=EXCLUDED.household_id,
                    child_id=EXCLUDED.child_id,
                    device_id=EXCLUDED.device_id,
                    session_id=EXCLUDED.session_id,
                    ts=EXCLUDED.ts,
                    kind=EXCLUDED.kind,
                    url=EXCLUDED.url,
                    normalized_url=EXCLUDED.normalized_url,
                    domain=EXCLUDED.domain,
                    title=EXCLUDED.title,
                    tab_id=EXCLUDED.tab_id,
                    referrer=EXCLUDED.referrer,
                    data_json=EXCLUDED.data_json,
                    raw_json=EXCLUDED.raw_json
                """,
                (
                    event_id,
                    household_id,
                    child_id,
                    event.get("device_id"),
                    event.get("session_id"),
                    event.get("ts"),
                    event.get("kind"),
                    event.get("url"),
                    normalized,
                    domain,
                    event.get("title"),
                    event.get("tab_id"),
                    event.get("referrer"),
                    Json(self._strip_screenshot_bytes(self._safe_json(event.get("data_json") or {}))),
                    Json(self._strip_screenshot_bytes(self._safe_json(event.get("data_json") or {}))),
                ),
            )
        return event_id

    def enqueue_event_job(self, event: Dict[str, Any], upgrade: bool = False) -> Tuple[str, str]:
        event_id = event.get("id") or f"evt_{uuid.uuid4().hex}"
        event["id"] = event_id
        job_id = f"job_{uuid.uuid4().hex}"
        now_ms = int(time.time() * 1000)
        household_id = event.get("household_id") or "hh_legacy"
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO event_jobs(id, household_id, event_id, event_json, upgrade, status, attempts, created_at)
                VALUES (%s, %s, %s, %s, %s, 'pending', 0, %s)
                """,
                (job_id, household_id, event_id, Json(event), upgrade, now_ms),
            )
        return job_id, event_id

    MAX_JOB_ATTEMPTS = 3
    STALE_JOB_SECONDS = 300

    def claim_event_jobs(self, limit: int = 5) -> List[Dict[str, Any]]:
        now_ms = int(time.time() * 1000)
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                WITH claimed AS (
                    SELECT id
                    FROM event_jobs
                    WHERE status='pending' AND attempts < %s
                    ORDER BY created_at ASC
                    LIMIT %s
                    FOR UPDATE SKIP LOCKED
                )
                UPDATE event_jobs j
                SET status='processing',
                    attempts=attempts + 1,
                    claimed_at=%s
                FROM claimed
                WHERE j.id=claimed.id
                RETURNING j.*
                """,
                (self.MAX_JOB_ATTEMPTS, limit, now_ms),
            )
            return cur.fetchall()

    def reap_stale_event_jobs(self) -> Dict[str, int]:
        """Recover jobs orphaned by a worker crash mid-processing: requeue them
        while attempts remain, dead-letter (status='failed') once exhausted.
        Returns counts for logging."""
        stale_before_ms = int(time.time() * 1000) - self.STALE_JOB_SECONDS * 1000
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE event_jobs
                SET status='pending', claimed_at=NULL
                WHERE status='processing' AND claimed_at < %s AND attempts < %s
                """,
                (stale_before_ms, self.MAX_JOB_ATTEMPTS),
            )
            requeued = cur.rowcount
            cur.execute(
                """
                UPDATE event_jobs
                SET status='failed', error='stale after max attempts'
                WHERE status='processing' AND claimed_at < %s AND attempts >= %s
                """,
                (stale_before_ms, self.MAX_JOB_ATTEMPTS),
            )
            dead_lettered = cur.rowcount
        return {"requeued": requeued, "dead_lettered": dead_lettered}

    def run_retention_sweep(self) -> Dict[str, int]:
        """Periodic privacy sweep (targets in docs/PRIVACY_LOGGING_AND_RETENTION.md).

        - DOM samples stripped from events after RETENTION_DOM_SAMPLE_DAYS (row kept).
        - Finished event_jobs (whole browsing payload copies) purged after
          RETENTION_EVENT_JOBS_DAYS.
        - Screenshot files + ocr_text deleted after RETENTION_SCREENSHOTS_DAYS.
        - Events deleted after RETENTION_EVENTS_DAYS; decisions/analysis/overrides
          ride along via ON DELETE CASCADE (they reference the event's context, so
          they cannot outlive it — the doc's separate 90-day URL target applies
          once decisions are self-contained).
        - Audit log and expired URL-cache rows purged last.
        Runs household-wide by design: retention is a platform guarantee, not a
        per-tenant query, and every deleted row is already past its window.
        """
        now_ms = int(time.time() * 1000)
        day_ms = 86_400_000
        counts: Dict[str, int] = {}
        screenshot_paths: List[str] = []
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE events
                SET data_json = (data_json - 'dom_sample') - 'screenshots_b64',
                    raw_json = (raw_json - 'dom_sample') - 'screenshots_b64'
                WHERE ts < %s
                  AND (data_json ?| array['dom_sample','screenshots_b64']
                       OR raw_json ?| array['dom_sample','screenshots_b64'])
                """,
                (now_ms - self.RETENTION_DOM_SAMPLE_DAYS * day_ms,),
            )
            counts["dom_samples_stripped"] = cur.rowcount
            cur.execute(
                "DELETE FROM event_jobs WHERE status IN ('completed','failed') AND created_at < %s",
                (now_ms - self.RETENTION_EVENT_JOBS_DAYS * day_ms,),
            )
            counts["event_jobs_purged"] = cur.rowcount
            cur.execute(
                """
                SELECT id, local_path FROM screenshot_files
                WHERE COALESCE(retention_expires_at, captured_at + make_interval(days => %s)) < now()
                """,
                (self.RETENTION_SCREENSHOTS_DAYS,),
            )
            expired_shots = cur.fetchall()
            if expired_shots:
                cur.execute(
                    "DELETE FROM screenshot_files WHERE id = ANY(%s)",
                    ([row["id"] for row in expired_shots],),
                )
                screenshot_paths = [row["local_path"] for row in expired_shots if row.get("local_path")]
            counts["screenshots_purged"] = len(expired_shots)
            cur.execute(
                "DELETE FROM events WHERE ts < %s",
                (now_ms - self.RETENTION_EVENTS_DAYS * day_ms,),
            )
            counts["events_purged"] = cur.rowcount
            cur.execute(
                "DELETE FROM audit_log WHERE created_at < now() - make_interval(days => %s)",
                (self.RETENTION_AUDIT_DAYS,),
            )
            counts["audit_purged"] = cur.rowcount
            cur.execute("DELETE FROM url_decision_cache WHERE expires_at IS NOT NULL AND expires_at < now()")
            counts["url_cache_purged"] = cur.rowcount
            # Rate-limit hits for scopes never checked again (one-off IPs) aren't
            # pruned inline — sweep anything older than an hour.
            cur.execute("DELETE FROM rate_limit_hits WHERE ts < %s", (now_ms - 3_600_000,))
            counts["rate_limit_hits_purged"] = cur.rowcount
        # Unlink files only after the rows are committed; a leftover file with no
        # row is re-swept as untracked noise, a row with no file would 404 forever.
        for path in screenshot_paths:
            try:
                Path(path).unlink(missing_ok=True)
            except OSError:
                pass
        return counts

    def get_queue_stats(self, household_id: str) -> Dict[str, Any]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT status, COUNT(*) AS count, MIN(created_at) AS oldest_created_at
                FROM event_jobs
                WHERE household_id=%s AND status IN ('pending', 'processing', 'failed')
                GROUP BY status
                """,
                (household_id,),
            )
            rows = {row["status"]: row for row in cur.fetchall()}
        now_ms = int(time.time() * 1000)
        pending = rows.get("pending")
        return {
            "pending": int((rows.get("pending") or {}).get("count") or 0),
            "processing": int((rows.get("processing") or {}).get("count") or 0),
            "failed": int((rows.get("failed") or {}).get("count") or 0),
            "oldest_pending_age_ms": (now_ms - int(pending["oldest_created_at"])) if pending and pending.get("oldest_created_at") else None,
        }

    def list_failed_event_jobs(self, household_id: str, limit: int = 20) -> List[Dict[str, Any]]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, event_id, attempts, error, created_at
                FROM event_jobs
                WHERE household_id=%s AND status='failed'
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (household_id, limit),
            )
            return cur.fetchall()

    def find_pending_upgrade_job(self, event_id: str) -> Optional[Dict[str, Any]]:
        # Server-side upgrade dedup: one screenshot job per event at a time
        # (SW eviction can make the extension re-upload despite its own dedup).
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, status FROM event_jobs
                WHERE event_id=%s AND upgrade=TRUE AND status IN ('pending','processing')
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (event_id,),
            )
            return cur.fetchone()

    def complete_event_job(self, job_id: str) -> None:
        now_ms = int(time.time() * 1000)
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE event_jobs SET status='completed', completed_at=%s, error=NULL WHERE id=%s",
                (now_ms, job_id),
            )

    def fail_event_job(self, job_id: str, error: str) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE event_jobs SET status='failed', error=%s WHERE id=%s",
                ((error or "")[:1000], job_id),
            )

    def update_event_data_json(self, event_id: str, data_json: str):
        stored = self._strip_screenshot_bytes(self._safe_json(data_json or {}))
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE events SET data_json=%s, raw_json=%s WHERE id=%s",
                (Json(stored), Json(stored), event_id),
            )

    def add_analysis(self, event_id: str, model: str, version: str, scores: Dict[str, Any], label: str = "", latency_ms: Optional[int] = None) -> str:
        analysis_id = f"ana_{uuid.uuid4().hex}"
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT household_id FROM events WHERE id=%s", (event_id,))
            event_row = cur.fetchone()
            household_id = (event_row or {}).get("household_id") or "hh_legacy"
            cur.execute(
                """
                INSERT INTO analysis(id, household_id, event_id, model, version, scores_json, label, latency_ms)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (analysis_id, household_id, event_id, model, version, Json(scores or {}), label, latency_ms),
            )
        return analysis_id

    def add_decision(self, event_id: str, policy_version: str, action: str, reason: str = "", details: Optional[Dict[str, Any]] = None) -> str:
        decision_id = f"dec_{uuid.uuid4().hex}"
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT household_id FROM events WHERE id=%s", (event_id,))
            event_row = cur.fetchone()
            household_id = (event_row or {}).get("household_id") or "hh_legacy"
            cur.execute(
                """
                INSERT INTO decisions(id, household_id, event_id, policy_version, action, reason, details_json, original_action)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (decision_id, household_id, event_id, policy_version, action, reason, Json(details or {}), action),
            )
        return decision_id

    def get_cached_url_decision(self, cache_key: str, ttl_seconds: int, household_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        min_updated_at = int(time.time() * 1000) - max(0, ttl_seconds) * 1000
        with self._connect() as conn, conn.cursor() as cur:
            params: List[Any] = [cache_key, min_updated_at]
            scope = ""
            if household_id:
                params.append(household_id)
                scope = " AND household_id=%s"
            cur.execute(
                f"""
                UPDATE url_decision_cache
                SET hit_count=hit_count + 1
                WHERE cache_key=%s AND updated_at >= %s
                {scope}
                RETURNING *
                """,
                params,
            )
            return cur.fetchone()

    def peek_url_decision(self, cache_key: str, ttl_seconds: int, household_id: str) -> Optional[Dict[str, Any]]:
        # Read-only cache lookup for the rule tester — must not bump hit_count.
        min_updated_at = int(time.time() * 1000) - max(0, ttl_seconds) * 1000
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM url_decision_cache WHERE cache_key=%s AND updated_at >= %s AND household_id=%s",
                (cache_key, min_updated_at, household_id),
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
        household_id: str = "hh_legacy",
    ) -> None:
        now_ms = int(time.time() * 1000)
        domain = self._domain(normalized_url)
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO url_decision_cache(
                    cache_key,
                    household_id,
                    normalized_url,
                    domain,
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
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 0, %s, %s)
                ON CONFLICT (cache_key) DO UPDATE SET
                    household_id=EXCLUDED.household_id,
                    normalized_url=EXCLUDED.normalized_url,
                    domain=EXCLUDED.domain,
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
                    household_id,
                    normalized_url,
                    domain,
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

    def get_recent_events(self, child_id: Optional[str], limit: int, household_id: Optional[str] = None):
        with self._connect() as conn, conn.cursor() as cur:
            clauses = []
            params: List[Any] = []
            if household_id:
                clauses.append("household_id=%s")
                params.append(household_id)
            if child_id:
                clauses.append("child_id=%s")
                params.append(child_id)
            where = " WHERE " + " AND ".join(clauses) if clauses else ""
            params.append(limit)
            cur.execute(f"SELECT * FROM events{where} ORDER BY ts DESC LIMIT %s", params)
            return cur.fetchall()

    def get_recent_decisions(self, child_id: Optional[str], limit: int, household_id: Optional[str] = None):
        base_query = """
            SELECT
                d.id,
                d.event_id,
                d.policy_version,
                COALESCE(o.action, d.action) AS action,
                d.reason,
                d.details_json,
                d.original_action,
                o.action AS manual_action,
                (o.id IS NOT NULL) AS manual_flagged,
                (o.processed_at IS NOT NULL) AS manual_processed,
                EXTRACT(EPOCH FROM o.created_at) * 1000 AS manual_updated_at,
                e.tab_id,
                e.url,
                e.title,
                e.ts,
                e.child_id,
                e.household_id
            FROM decisions d
            JOIN events e ON d.event_id = e.id
            LEFT JOIN LATERAL (
                SELECT *
                FROM decision_overrides o
                WHERE o.decision_id=d.id
                ORDER BY o.created_at DESC
                LIMIT 1
            ) o ON TRUE
        """
        with self._connect() as conn, conn.cursor() as cur:
            clauses = []
            params: List[Any] = []
            if household_id:
                clauses.append("e.household_id=%s")
                params.append(household_id)
            if child_id:
                clauses.append("e.child_id=%s")
                params.append(child_id)
            where = " WHERE " + " AND ".join(clauses) if clauses else ""
            params.append(limit)
            cur.execute(base_query + where + " ORDER BY e.ts DESC LIMIT %s", params)
            return cur.fetchall()

    def list_review_decisions(self, household_id: str, child_id: Optional[str] = None, limit: int = 30) -> List[Dict[str, Any]]:
        """Guardian review queue: recent decisions (7 days) worth a look with no
        override yet — restrictive actions, pending OCR, system_uncertain
        degradations, low-confidence judgments. Severity first, then recency."""
        since_ms = int(time.time() * 1000) - 7 * 24 * 3600 * 1000
        clauses = ["e.household_id=%s", "e.ts >= %s", "o.id IS NULL"]
        params: List[Any] = [household_id, since_ms]
        if child_id:
            clauses.append("e.child_id=%s")
            params.append(child_id)
        params.append(limit)
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT
                    d.id, d.event_id, d.action, d.reason, d.details_json, d.original_action,
                    e.url, e.title, e.domain, e.ts, e.child_id, e.device_id
                FROM decisions d
                JOIN events e ON d.event_id=e.id
                LEFT JOIN LATERAL (
                    SELECT id FROM decision_overrides o
                    WHERE o.decision_id=d.id
                    ORDER BY o.created_at DESC
                    LIMIT 1
                ) o ON TRUE
                WHERE {" AND ".join(clauses)}
                  AND (
                    d.action IN ('block','blur','warn','notify')
                    OR d.reason IN ('pending_ocr','system_uncertain')
                    OR COALESCE((d.details_json->>'confidence')::float, 1.0) < 0.5
                  )
                ORDER BY
                    CASE d.action WHEN 'block' THEN 0 WHEN 'blur' THEN 1 WHEN 'warn' THEN 2 ELSE 3 END,
                    e.ts DESC
                LIMIT %s
                """,
                params,
            )
            return cur.fetchall()

    def get_decision_with_event(self, decision_id: str, household_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        with self._connect() as conn, conn.cursor() as cur:
            params: List[Any] = [decision_id]
            scope = ""
            if household_id:
                params.append(household_id)
                scope = " AND d.household_id=%s"
            cur.execute(
                f"""
                SELECT
                    d.*,
                    COALESCE(o.action, d.action) AS action,
                    o.action AS manual_action,
                    (o.id IS NOT NULL) AS manual_flagged,
                    (o.processed_at IS NOT NULL) AS manual_processed,
                    EXTRACT(EPOCH FROM o.created_at) * 1000 AS manual_updated_at,
                    e.url, e.title, e.ts, e.child_id, e.tab_id, e.device_id, e.session_id
                FROM decisions d
                JOIN events e ON d.event_id=e.id
                LEFT JOIN LATERAL (
                    SELECT *
                    FROM decision_overrides o
                    WHERE o.decision_id=d.id
                    ORDER BY o.created_at DESC
                    LIMIT 1
                ) o ON TRUE
                WHERE d.id=%s
                {scope}
                """,
                params,
            )
            return cur.fetchone()

    def get_decision_by_event(self, event_id: str, household_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Latest decision for an event, scoped to a household. Used by the
        extension to poll for the enforcement decision it must apply."""
        with self._connect() as conn, conn.cursor() as cur:
            params: List[Any] = [event_id]
            scope = ""
            if household_id:
                params.append(household_id)
                scope = " AND d.household_id=%s"
            cur.execute(
                f"""
                SELECT
                    d.*,
                    COALESCE(o.action, d.action) AS action,
                    o.action AS manual_action,
                    (o.id IS NOT NULL) AS manual_flagged,
                    EXTRACT(EPOCH FROM o.created_at) * 1000 AS manual_updated_at,
                    e.url, e.title, e.ts, e.child_id, e.tab_id, e.device_id, e.session_id
                FROM decisions d
                JOIN events e ON d.event_id=e.id
                LEFT JOIN LATERAL (
                    SELECT *
                    FROM decision_overrides o
                    WHERE o.decision_id=d.id
                    ORDER BY o.created_at DESC
                    LIMIT 1
                ) o ON TRUE
                WHERE d.event_id=%s
                {scope}
                ORDER BY d.created_at DESC
                LIMIT 1
                """,
                params,
            )
            return cur.fetchone()

    def override_decision(
        self,
        decision_id: str,
        new_action: str,
        household_id: Optional[str] = None,
        guardian_id: Optional[str] = None,
        reason: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT household_id FROM decisions WHERE id=%s" + (" AND household_id=%s" if household_id else ""),
                (decision_id, household_id) if household_id else (decision_id,),
            )
            decision = cur.fetchone()
            if not decision:
                return None
            cur.execute(
                """
                INSERT INTO decision_overrides(id, household_id, decision_id, guardian_id, action, reason)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (self._new_id("ovr"), decision["household_id"], decision_id, guardian_id, new_action, reason),
            )
        return self.get_decision_with_event(decision_id, household_id)

    def fetch_unprocessed_overrides(self, limit: int = 50, household_id: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._connect() as conn, conn.cursor() as cur:
            params: List[Any] = []
            scope = ""
            if household_id:
                params.append(household_id)
                scope = " AND o.household_id=%s"
            params.append(limit)
            cur.execute(
                f"""
                SELECT o.id AS override_id, o.action AS manual_action, o.processed_at, o.created_at AS manual_updated_at,
                       d.*, e.url, e.title, e.ts, e.child_id
                FROM decision_overrides o
                JOIN decisions d ON o.decision_id=d.id
                JOIN events e ON d.event_id = e.id
                WHERE o.processed_at IS NULL
                {scope}
                ORDER BY o.created_at DESC
                LIMIT %s
                """,
                params,
            )
            return cur.fetchall()

    def mark_override_processed(self, override_ids: List[str]) -> None:
        if not override_ids:
            return
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("UPDATE decision_overrides SET processed_at=now() WHERE id = ANY(%s)", (override_ids,))

    def get_setting(self, key: str, household_id: str = "hh_legacy") -> Optional[str]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT value_json FROM household_settings WHERE household_id=%s AND key=%s", (household_id, key))
            row = cur.fetchone()
            if not row:
                return None
            value_json = row["value_json"] or {}
            if isinstance(value_json, dict) and "value" in value_json:
                return str(value_json["value"])
            return json.dumps(value_json)

    def set_setting(self, key: str, value: str, household_id: str = "hh_legacy", guardian_id: Optional[str] = None) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO household_settings(id, household_id, key, value_json, updated_by_guardian_id, updated_at)
                VALUES (%s, %s, %s, %s, %s, now())
                ON CONFLICT (household_id, key) DO UPDATE SET
            value_json=EXCLUDED.value_json,
                    updated_by_guardian_id=EXCLUDED.updated_by_guardian_id,
                    updated_at=now()
                """,
                (self._new_id("hset"), household_id, key, Json({"value": value}), guardian_id),
            )

    def get_household_setting_json(self, key: str, household_id: str = "hh_legacy") -> Optional[Dict[str, Any]]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT value_json FROM household_settings WHERE household_id=%s AND key=%s", (household_id, key))
            row = cur.fetchone()
            if not row:
                return None
            value = row["value_json"]
            return value if isinstance(value, dict) else None

    def set_household_setting_json(
        self,
        key: str,
        value: Dict[str, Any],
        household_id: str = "hh_legacy",
        guardian_id: Optional[str] = None,
    ) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO household_settings(id, household_id, key, value_json, updated_by_guardian_id, updated_at)
                VALUES (%s, %s, %s, %s, %s, now())
                ON CONFLICT (household_id, key) DO UPDATE SET
                    value_json=EXCLUDED.value_json,
                    updated_by_guardian_id=EXCLUDED.updated_by_guardian_id,
                    updated_at=now()
                """,
                (self._new_id("hset"), household_id, key, Json(value), guardian_id),
            )

    def is_household_monitoring_enabled(self, household_id: str = "hh_legacy") -> bool:
        # Household-wide monitoring switch. Absent setting => enabled by default.
        setting = self.get_household_setting_json("monitoring_enabled", household_id)
        if not setting:
            return True
        return bool(setting.get("enabled", True))

    def set_household_monitoring_enabled(
        self, enabled: bool, household_id: str = "hh_legacy", guardian_id: Optional[str] = None
    ) -> None:
        self.set_household_setting_json("monitoring_enabled", {"enabled": bool(enabled)}, household_id, guardian_id)

    def is_parent_pin_set(self, household_id: str = "hh_legacy") -> bool:
        pin = self.get_household_setting_json(self.PARENT_PIN_KEY, household_id)
        return bool(
            pin
            and pin.get("algorithm") == self.PARENT_PIN_ALGORITHM
            and pin.get("salt")
            and pin.get("hash")
        )

    def set_parent_pin(self, pin: str, household_id: str = "hh_legacy", guardian_id: Optional[str] = None) -> None:
        salt = secrets.token_urlsafe(24)
        digest = hashlib.pbkdf2_hmac(
            "sha256",
            pin.encode("utf-8"),
            salt.encode("utf-8"),
            self.PARENT_PIN_ITERATIONS,
        ).hex()
        payload = {
            "algorithm": self.PARENT_PIN_ALGORITHM,
            "iterations": self.PARENT_PIN_ITERATIONS,
            "salt": salt,
            "hash": digest,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        self.set_household_setting_json(self.PARENT_PIN_KEY, payload, household_id, guardian_id)

    def verify_parent_pin(self, pin: str, household_id: str = "hh_legacy") -> bool:
        stored = self.get_household_setting_json(self.PARENT_PIN_KEY, household_id)
        if not stored or stored.get("algorithm") != self.PARENT_PIN_ALGORITHM:
            return False
        try:
            iterations = int(stored.get("iterations") or self.PARENT_PIN_ITERATIONS)
            salt = str(stored["salt"])
            expected = str(stored["hash"])
        except (KeyError, TypeError, ValueError):
            return False
        actual = hashlib.pbkdf2_hmac(
            "sha256",
            pin.encode("utf-8"),
            salt.encode("utf-8"),
            iterations,
        ).hex()
        return hmac.compare_digest(actual, expected)

    def delete_setting(self, key: str, household_id: str = "hh_legacy") -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM household_settings WHERE household_id=%s AND key=%s", (household_id, key))

    def get_active_child_id(self, household_id: str = "hh_legacy") -> Optional[str]:
        return self.get_setting("active_child_id", household_id)

    def set_active_child_id(self, child_id: str, household_id: str = "hh_legacy", guardian_id: Optional[str] = None) -> None:
        self.set_setting("active_child_id", child_id, household_id, guardian_id)

    def get_paused_until(self, household_id: str = "hh_legacy") -> Optional[int]:
        value = self.get_setting("paused_until", household_id)
        if not value:
            return None
        try:
            return int(value)
        except ValueError:
            return None

    def create_pairing_code(self, household_id: str, child_id: str, guardian_id: str, ttl_minutes: int = 15) -> Dict[str, Any]:
        code = f"{secrets.randbelow(1000000):06d}"
        code_hash = self._token_hash(code)
        row_id = self._new_id("pair")
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO device_pairing_codes(id, household_id, child_id, code_hash, expires_at, created_by_guardian_id)
                VALUES (%s, %s, %s, %s, now() + (%s || ' minutes')::interval, %s)
                RETURNING id, household_id, child_id, expires_at, created_at
                """,
                (row_id, household_id, child_id, code_hash, ttl_minutes, guardian_id),
            )
            row = cur.fetchone()
        return {**row, "code": code}

    def redeem_pairing_code(
        self,
        code: str,
        *,
        install_id: str,
        device_name: str = "",
        browser_name: str = "",
        browser_version: str = "",
        extension_version: str = "",
    ) -> Optional[Dict[str, Any]]:
        code_hash = self._token_hash(code)
        device_token = f"wdev_{secrets.token_urlsafe(32)}"
        device_id = self._new_id("dev")
        token_hash = self._token_hash(device_token)
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE device_pairing_codes
                SET redeemed_at=now()
                WHERE code_hash=%s AND redeemed_at IS NULL AND expires_at > now()
                RETURNING *
                """,
                (code_hash,),
            )
            pairing = cur.fetchone()
            if not pairing:
                return None
            # Same physical device (unique install_id) may already be paired to
            # another child. Capture the prior owner before the upsert overwrites it
            # so we can stop its stale session and audit the reassignment.
            cur.execute(
                "SELECT id, child_id, household_id FROM devices WHERE install_id=%s",
                (install_id,),
            )
            prior = cur.fetchone()
            cur.execute(
                """
                INSERT INTO devices(
                    id, household_id, child_id, device_name, device_type, browser_name, browser_version,
                    extension_version, install_id, token_hash, token_expires_at, status, last_seen_at
                )
                VALUES (%s, %s, %s, %s, 'browser_extension', %s, %s, %s, %s, %s,
                        now() + make_interval(days => %s), 'active', now())
                ON CONFLICT (install_id) DO UPDATE SET
                    household_id=EXCLUDED.household_id,
                    child_id=EXCLUDED.child_id,
                    device_name=EXCLUDED.device_name,
                    browser_name=EXCLUDED.browser_name,
                    browser_version=EXCLUDED.browser_version,
                    extension_version=EXCLUDED.extension_version,
                    token_hash=EXCLUDED.token_hash,
                    token_expires_at=EXCLUDED.token_expires_at,
                    token_prev_hash=NULL,
                    token_rotated_at=NULL,
                    status='active',
                    last_seen_at=now(),
                    updated_at=now()
                RETURNING *
                """,
                (
                    device_id,
                    pairing["household_id"],
                    pairing["child_id"],
                    device_name,
                    browser_name,
                    browser_version,
                    extension_version,
                    install_id,
                    token_hash,
                    self.DEVICE_TOKEN_TTL_DAYS,
                ),
            )
            device = cur.fetchone()
            reassigned = None
            if prior and prior["child_id"] != pairing["child_id"]:
                # Stop the previous child's active session for this exact device
                # (scoped by device_id so the child's other devices keep running).
                cur.execute(
                    """
                    UPDATE monitoring_sessions
                    SET status='stopped', stopped_at=now(), stop_reason='device_reassigned'
                    WHERE child_id=%s AND device_id=%s AND status='active'
                    """,
                    (prior["child_id"], prior["id"]),
                )
                reassigned = {
                    "from_child_id": prior["child_id"],
                    "from_household_id": prior["household_id"],
                    "to_child_id": pairing["child_id"],
                    "to_household_id": pairing["household_id"],
                }
            # Protection by default: pairing implies monitoring. Start a session
            # scoped to this device only — never touch the child's sessions on
            # other devices. Pairing is an explicit guardian action (they minted
            # the code), so this applies even after a previous guardian stop.
            session_started = None
            cur.execute(
                """
                SELECT id FROM monitoring_sessions
                WHERE household_id=%s AND child_id=%s AND status='active'
                  AND (device_id=%s OR device_id IS NULL)
                LIMIT 1
                """,
                (pairing["household_id"], pairing["child_id"], device["id"]),
            )
            if not cur.fetchone():
                cur.execute(
                    """
                    INSERT INTO monitoring_sessions(id, household_id, child_id, device_id, started_by_guardian_id, status)
                    VALUES (%s, %s, %s, %s, %s, 'active')
                    RETURNING *
                    """,
                    (self._new_id("sess"), pairing["household_id"], pairing["child_id"], device["id"], pairing.get("created_by_guardian_id")),
                )
                session_started = cur.fetchone()
        return {"device": device, "device_token": device_token, "reassigned": reassigned, "session": session_started}

    def resolve_device_token(self, token: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        """Resolve a raw device token to (device, error).

        error is None on success, else one of: 'invalid', 'device_revoked',
        'token_expired'. Distinct errors let the extension tell "re-pair needed"
        apart from "rotate needed". A token rotated out within the grace window
        still authenticates.
        """
        token_hash = self._token_hash(token)
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT *,
                       (token_hash=%s) AS matched_current,
                       (token_prev_hash=%s
                        AND token_rotated_at > now() - make_interval(mins => %s)) AS matched_prev
                FROM devices
                WHERE token_hash=%s OR token_prev_hash=%s
                """,
                (token_hash, token_hash, self.TOKEN_ROTATION_GRACE_MINUTES, token_hash, token_hash),
            )
            device = cur.fetchone()
            if not device or not (device["matched_current"] or device["matched_prev"]):
                return None, "invalid"
            if device.get("status") != "active":
                return None, "device_revoked"
            expires_at = device.get("token_expires_at")
            if device["matched_current"] and expires_at is not None and expires_at <= datetime.now(timezone.utc):
                return None, "token_expired"
            cur.execute(
                "UPDATE devices SET last_seen_at=now(), updated_at=now() WHERE id=%s",
                (device["id"],),
            )
            device.pop("matched_current", None)
            device.pop("matched_prev", None)
            return device, None

    def authenticate_device_token(self, token: str) -> Optional[Dict[str, Any]]:
        device, error = self.resolve_device_token(token)
        return device if error is None else None

    def rotate_device_token(self, device_id: str) -> Optional[Tuple[Dict[str, Any], str]]:
        """Issue a fresh token for an active device; the old one keeps working
        for TOKEN_ROTATION_GRACE_MINUTES. Returns (device_row, new_raw_token)."""
        new_token = f"wdev_{secrets.token_urlsafe(32)}"
        new_hash = self._token_hash(new_token)
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE devices
                SET token_prev_hash=token_hash,
                    token_rotated_at=now(),
                    token_hash=%s,
                    token_expires_at=now() + make_interval(days => %s),
                    updated_at=now()
                WHERE id=%s AND status='active'
                RETURNING *
                """,
                (new_hash, self.DEVICE_TOKEN_TTL_DAYS, device_id),
            )
            device = cur.fetchone()
        if not device:
            return None
        return device, new_token

    def get_event_household(self, event_id: str) -> Optional[str]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT household_id FROM events WHERE id=%s", (event_id,))
            row = cur.fetchone()
            return row["household_id"] if row else None

    def start_monitoring_session(
        self,
        household_id: str,
        child_id: str,
        guardian_id: str,
        device_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE monitoring_sessions
                SET status='stopped', stopped_by_guardian_id=%s, stopped_at=now(), stop_reason='replaced'
                WHERE household_id=%s AND child_id=%s AND status='active'
                """,
                (guardian_id, household_id, child_id),
            )
            cur.execute(
                """
                INSERT INTO monitoring_sessions(id, household_id, child_id, device_id, started_by_guardian_id, status)
                VALUES (%s, %s, %s, %s, %s, 'active')
                RETURNING *
                """,
                (self._new_id("sess"), household_id, child_id, device_id, guardian_id),
            )
            return cur.fetchone()

    def stop_monitoring_session(
        self,
        household_id: str,
        child_id: Optional[str],
        guardian_id: str,
        reason: str = "guardian_stopped",
    ) -> int:
        with self._connect() as conn, conn.cursor() as cur:
            if child_id:
                cur.execute(
                    """
                    UPDATE monitoring_sessions
                    SET status='stopped', stopped_by_guardian_id=%s, stopped_at=now(), stop_reason=%s
                    WHERE household_id=%s AND child_id=%s AND status='active'
                    """,
                    (guardian_id, reason, household_id, child_id),
                )
            else:
                cur.execute(
                    """
                    UPDATE monitoring_sessions
                    SET status='stopped', stopped_by_guardian_id=%s, stopped_at=now(), stop_reason=%s
                    WHERE household_id=%s AND status='active'
                    """,
                    (guardian_id, reason, household_id),
                )
            return cur.rowcount

    def ensure_monitoring_session(
        self,
        household_id: str,
        child_id: str,
        device_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Protection by default: return the active session, auto-starting one
        when none exists — unless the most recent session for this child was
        explicitly stopped by a guardian (their off switch stays an off switch).
        Auto-started sessions have no starting guardian and are audited."""
        session = self.get_active_monitoring_session(household_id, child_id, device_id)
        if session:
            return session
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT stop_reason FROM monitoring_sessions
                WHERE household_id=%s AND child_id=%s
                ORDER BY started_at DESC
                LIMIT 1
                """,
                (household_id, child_id),
            )
            last = cur.fetchone()
            if last and last.get("stop_reason") == "guardian_stopped":
                return None
            cur.execute(
                """
                INSERT INTO monitoring_sessions(id, household_id, child_id, device_id, status)
                VALUES (%s, %s, %s, %s, 'active')
                RETURNING *
                """,
                (self._new_id("sess"), household_id, child_id, device_id),
            )
            session = cur.fetchone()
        self.log_audit(
            household_id,
            "monitoring_autostarted",
            device_id=device_id,
            entity_type="monitoring_session",
            entity_id=session["id"],
            metadata={"child_id": child_id},
        )
        return session

    def get_active_monitoring_session(
        self,
        household_id: str,
        child_id: str,
        device_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        with self._connect() as conn, conn.cursor() as cur:
            params: List[Any] = [household_id, child_id]
            device_scope = ""
            if device_id:
                params.append(device_id)
                device_scope = " AND (device_id=%s OR device_id IS NULL)"
            cur.execute(
                f"""
                SELECT *
                FROM monitoring_sessions
                WHERE household_id=%s AND child_id=%s AND status='active'
                {device_scope}
                ORDER BY started_at DESC
                LIMIT 1
                """,
                params,
            )
            return cur.fetchone()

    def insert_screenshot_file(
        self,
        *,
        household_id: str,
        child_id: Optional[str],
        device_id: Optional[str],
        event_id: str,
        session_id: Optional[str],
        local_path: str,
        sha256: Optional[str],
        size_bytes: Optional[int],
        ocr_text: Optional[str] = None,
    ) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO screenshot_files(
                    id, household_id, child_id, device_id, event_id, session_id, local_path, sha256, size_bytes, ocr_text, captured_at, retention_expires_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now(), now() + make_interval(days => %s))
                """,
                (self._new_id("shot"), household_id, child_id, device_id, event_id, session_id, local_path, sha256, size_bytes, ocr_text, self.RETENTION_SCREENSHOTS_DAYS),
            )

    @staticmethod
    def _safe_json(value: Any) -> Any:
        if isinstance(value, str):
            try:
                return json.loads(value) if value else {}
            except Exception:
                return {"raw": value}
        return value or {}


db = Database()
