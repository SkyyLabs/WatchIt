from __future__ import annotations

import logging
import logging.handlers
import queue
import sys
from typing import Any

import structlog

from watchit_core.config import settings

_configured = False
_listener: logging.handlers.QueueListener | None = None


class _PassThroughQueueHandler(logging.handlers.QueueHandler):
    """Enqueue the record untouched.

    The stdlib ``QueueHandler.prepare`` formats the record to a string before
    enqueuing (meant for pickling across processes). That would stringify
    structlog's event-dict ``record.msg`` and break ``ProcessorFormatter`` on
    the listener side. Same-process threads don't need pickling, so pass the
    record through unchanged and let the listener's formatter render it.
    """

    def prepare(self, record: logging.LogRecord) -> logging.LogRecord:
        return record


def configure_logging(service: str) -> None:
    """Configure JSON stdout logging for API and worker processes."""
    global _configured
    if _configured:
        structlog.contextvars.bind_contextvars(service=service)
        return

    timestamper = structlog.processors.TimeStamper(fmt="iso", utc=True)
    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        timestamper,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(),
        ],
    )
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)

    # Non-blocking logging: the hot path only enqueues a record (QueueHandler);
    # a background thread (QueueListener) does the formatting + stdout write, so
    # log calls never add request/pipeline latency.
    global _listener
    log_queue: queue.Queue[Any] = queue.Queue(-1)
    _listener = logging.handlers.QueueListener(log_queue, stream_handler, respect_handler_level=True)
    _listener.start()

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(_PassThroughQueueHandler(log_queue))
    root.setLevel(settings.log_level.upper())

    for logger_name in ("uvicorn", "uvicorn.error", "uvicorn.access", "fastapi"):
        logging.getLogger(logger_name).handlers.clear()
        logging.getLogger(logger_name).propagate = True

    # Quiet chatty third-party loggers so the terminal shows our signal, not
    # per-request access lines, file-watch churn, or per-startup migration noise.
    for noisy in ("uvicorn.access", "watchfiles", "watchfiles.main", "alembic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    structlog.contextvars.bind_contextvars(service=service)
    _configured = True


def shutdown_logging() -> None:
    """Stop the background log listener, draining any queued records. Idempotent."""
    global _listener, _configured
    if _listener is not None:
        _listener.stop()
        _listener = None
    _configured = False


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)


def bind_log_context(**kwargs: Any) -> None:
    structlog.contextvars.bind_contextvars(**{k: v for k, v in kwargs.items() if v is not None})


def clear_log_context() -> None:
    structlog.contextvars.clear_contextvars()
