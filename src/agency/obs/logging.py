"""Structured logging with run_id correlation.

Every log line emitted during a run carries the same `run_id`, so a run's full tool
trace can be reconstructed with a single grep.
"""

from __future__ import annotations

import contextvars
import logging
import sys
from collections.abc import Iterator, MutableMapping
from contextlib import contextmanager
from typing import Any

import structlog

_run_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("run_id", default=None)
_configured = False


def _inject_run_id(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    run_id = _run_id.get()
    if run_id:
        event_dict["run_id"] = run_id
    return event_dict


def configure_logging(level: str = "INFO", fmt: str = "console") -> None:
    """Idempotent logging setup. Call once at process start."""
    global _configured
    if _configured:
        return

    logging.basicConfig(format="%(message)s", stream=sys.stderr, level=level.upper())

    renderer: Any = (
        structlog.dev.ConsoleRenderer()
        if fmt == "console"
        else structlog.processors.JSONRenderer()
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            _inject_run_id,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        cache_logger_on_first_use=True,
    )
    _configured = True


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    if not _configured:
        configure_logging()
    return structlog.get_logger(name)  # type: ignore[no-any-return]


@contextmanager
def run_context(run_id: str) -> Iterator[None]:
    token = _run_id.set(run_id)
    try:
        yield
    finally:
        _run_id.reset(token)


def current_run_id() -> str | None:
    return _run_id.get()
