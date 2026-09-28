"""structlog setup. Console renderer for terminals, JSON for journald."""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog


class _StderrLogger:
    """Writes to whatever sys.stderr is at the moment of the call, not at configure time."""

    def msg(self, message: str) -> None:
        print(message, file=sys.stderr, flush=True)

    log = debug = info = warning = error = critical = exception = fatal = msg


def _stderr_factory(*_args: Any) -> _StderrLogger:
    return _StderrLogger()


def configure_logging(level: str = "info", fmt: str = "auto") -> None:
    numeric = logging.getLevelName(level.upper())
    if not isinstance(numeric, int):
        numeric = logging.INFO
    if fmt == "auto":
        fmt = "console" if sys.stderr.isatty() else "json"
    renderer: structlog.types.Processor = (
        structlog.processors.JSONRenderer() if fmt == "json" else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(numeric),
        logger_factory=_stderr_factory,
        cache_logger_on_first_use=False,
    )
    logging.basicConfig(level=numeric, stream=sys.stderr, format="%(message)s")


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)  # type: ignore[no-any-return]
