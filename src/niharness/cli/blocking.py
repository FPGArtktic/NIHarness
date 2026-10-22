# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""Running the async core from a synchronous Typer command.

The CLI is the only synchronous edge of the harness, so it is the only place
that starts an event loop - and the only place that has to undo what anyio's
task groups do to an exception on the way out. A command's `except` clauses are
written against the failure (`httpx.HTTPError`, `ExternalToolsError`), not
against the `BaseExceptionGroup` the transport wraps it in, so the wrapper is
removed here rather than in every handler.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any

from niharness.adapters.mcp import unwrap_group


def blocking[T](coroutine: Coroutine[Any, Any, T]) -> T:
    """Run `coroutine` to completion and return its result.

    Args:
        coroutine: the async entry point to run.

    Returns:
        Whatever it returned.

    Raises:
        BaseException: whatever it raised, with the task-group wrappers
            removed, so the caller's handlers see the failure itself.
    """
    try:
        return asyncio.run(coroutine)
    except BaseExceptionGroup as group:
        raise unwrap_group(group) from None
