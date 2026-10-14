# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""Structured logging setup.

Logs are JSON on stderr: every step and tool call is a span, and nothing in the
harness uses `print`. The CLI writes its human output with Typer instead.
"""

from __future__ import annotations

import logging
import sys

import structlog


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
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )
