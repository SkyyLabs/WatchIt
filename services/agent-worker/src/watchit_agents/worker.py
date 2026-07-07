from __future__ import annotations

import asyncio
import json
import time

from watchit_core.config import settings
from watchit_core.db import db
from watchit_core.activity_logger import log_service_event
from watchit_core.logging import bind_log_context, clear_log_context, configure_logging, get_logger, shutdown_logging
from watchit_agents.runtime import process_event

configure_logging("agent-worker")
logger = get_logger("watchit.agent_worker")


class AgentWorker:
    """Consumes queued browser events and runs the safety pipeline."""

    REAP_INTERVAL_SECONDS = 60.0

    def __init__(self, poll_interval: float | None = None, batch_size: int = 5):
        self.poll_interval = poll_interval if poll_interval is not None else settings.agent_worker_poll_interval
        self.batch_size = batch_size
        self._last_reap = 0.0

    async def run_forever(self) -> None:
        bind_log_context(service="agent-worker")
        db.connect()
        log_service_event("agent_worker_started", {"poll_interval": self.poll_interval, "batch_size": self.batch_size})
        logger.info("agent_worker_started", poll_interval=self.poll_interval, batch_size=self.batch_size)
        while True:
            try:
                self.maybe_reap_stale_jobs()
                await self.process_once()
            except asyncio.CancelledError:
                logger.info("agent_worker_cancelled")
                raise
            except Exception:
                logger.exception("agent_worker_loop_failed")
            await asyncio.sleep(self.poll_interval)

    def maybe_reap_stale_jobs(self) -> None:
        # Recover jobs orphaned by a worker crash: requeue while attempts remain,
        # dead-letter after. Time-gated so the sweep doesn't run on every poll.
        now = time.monotonic()
        if now - self._last_reap < self.REAP_INTERVAL_SECONDS:
            return
        self._last_reap = now
        counts = db.reap_stale_event_jobs()
        if counts["requeued"] or counts["dead_lettered"]:
            logger.warning("stale_event_jobs_reaped", **counts)

    async def process_once(self) -> int:
        claim_started = time.perf_counter()
        jobs = db.claim_event_jobs(self.batch_size)
        claim_duration_ms = round((time.perf_counter() - claim_started) * 1000, 2)
        if jobs:
            logger.debug("event_jobs_claimed", count=len(jobs), claim_duration_ms=claim_duration_ms)
        for job in jobs:
            clear_log_context()
            created_at = int(job.get("created_at") or 0)
            claimed_at = int(job.get("claimed_at") or int(time.time() * 1000))
            queue_wait_ms = max(0, claimed_at - created_at) if created_at else None
            job_started = time.perf_counter()
            bind_log_context(
                service="agent-worker",
                job_id=job.get("id"),
                event_id=job.get("event_id"),
                upgrade=bool(job.get("upgrade")),
                queue_wait_ms=queue_wait_ms,
            )
            try:
                event_payload = job["event_json"]
                event = event_payload if isinstance(event_payload, dict) else json.loads(event_payload)
                bind_log_context(child_id=event.get("child_id"), tab_id=event.get("tab_id"))
                logger.debug("event_job_started", attempts=job.get("attempts"), queue_wait_ms=queue_wait_ms)
                await process_event(event, upgrade=bool(job.get("upgrade")))
                db.complete_event_job(job["id"])
                processing_duration_ms = round((time.perf_counter() - job_started) * 1000, 2)
                logger.debug("event_job_completed", processing_duration_ms=processing_duration_ms, queue_wait_ms=queue_wait_ms)
            except Exception as exc:
                processing_duration_ms = round((time.perf_counter() - job_started) * 1000, 2)
                logger.exception("event_job_failed", processing_duration_ms=processing_duration_ms, queue_wait_ms=queue_wait_ms)
                db.fail_event_job(job["id"], str(exc))
            finally:
                clear_log_context()
        return len(jobs)


async def main() -> None:
    try:
        await AgentWorker().run_forever()
    finally:
        # Drain the async log listener so shutdown records still reach stdout.
        shutdown_logging()


if __name__ == "__main__":
    asyncio.run(main())
