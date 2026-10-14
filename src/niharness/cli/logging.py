# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""Structured logging setup.

Logs are JSON on stderr: every step and tool call is a span, and nothing in the
harness uses `print`. The CLI writes its human output with Typer instead.

The stream is resolved at write time rather than at configuration time. Binding
it once looks harmless until something replaces `sys.stderr` after the harness
has configured itself - a test runner capturing output, an embedding process -
and every later log line raises on a closed file.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog


class StderrLogger:
    """A structlog logger that writes to whatever `sys.stderr` is right now."""

    def msg(self, message: str) -> None:
        """Write one rendered record."""
        stream = sys.stderr
        stream.write(f"{message}\n")
        stream.flush()

    log = debug = info = warn = warning = msg
    fatal = error = critical = exception = msg

    def __repr__(self) -> str:
        return "<StderrLogger>"


class StderrLoggerFactory:
    """Hands structlog a :class:`StderrLogger`."""

    def __call__(self, *args: Any) -> StderrLogger:  # noqa: ANN401 - structlog passes the name
        """Return a logger; the arguments structlog passes are not needed."""
        del args
        return StderrLogger()


def configure(verbose: bool = False) -> None:
    """Configure structlog for one CLI invocation.

    Args:
        verbose: emit debug records as well as info and above.
    """
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(format="%(message)s", stream=sys.stderr, level=level)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=StderrLoggerFactory(),
        cache_logger_on_first_use=True,
    )
