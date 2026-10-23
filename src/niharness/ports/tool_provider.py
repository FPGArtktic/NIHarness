# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""The tool port.

Tools are MCP tools, normally executed inside the sandbox container. The
provider never sees an unvalidated call: the domain validates first, and only
an :class:`~niharness.domain.tools.AcceptedCall` reaches this port.

A few tools cannot run in the sandbox, because the sandbox has no network and
they need a service on the host - `rag_query` and the vector store. Those are
served by a :class:`LocalToolProvider`, which is the same port plus a statement
of which names it answers, so the router can send them there instead of into
the container.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from niharness.domain.messages import ToolSchema
from niharness.domain.tools import AcceptedCall, ToolId, ToolResult


@runtime_checkable
class ToolProvider(Protocol):
    """Advertises tool schemas and executes validated calls."""

    async def descriptors(self, allowlist: frozenset[ToolId]) -> tuple[ToolSchema, ...]:
        """Return the schemas of the allowed tools, for the model's tool list."""
        ...

    async def call(self, accepted: AcceptedCall) -> ToolResult:
        """Execute a validated call in the sandbox and return a truncated result."""
        ...


@runtime_checkable
class LocalToolProvider(ToolProvider, Protocol):
    """A provider that runs in the harness process and names the tools it answers.

    The names are declared rather than discovered: a provider cannot claim a
    tool the router did not expect it to claim, least of all one the sandbox
    holds - `git_commit` is the gate and the gate is in the container.
    """

    @property
    def served(self) -> frozenset[ToolId]:
        """The tool names this provider answers."""
        ...
