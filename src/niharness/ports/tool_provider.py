# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""The tool port.

Tools are MCP tools executed inside the sandbox container. The provider never
sees an unvalidated call: the domain validates first, and only an
:class:`~niharness.domain.tools.AcceptedCall` reaches this port.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from niharness.domain.messages import ToolSchema
from niharness.domain.tools import AcceptedCall, ToolName, ToolResult


@runtime_checkable
class ToolProvider(Protocol):
    """Advertises tool schemas and executes validated calls."""

    async def descriptors(self, allowlist: frozenset[ToolName]) -> tuple[ToolSchema, ...]:
        """Return the schemas of the allowed tools, for the model's tool list."""
        ...

    async def call(self, accepted: AcceptedCall) -> ToolResult:
        """Execute a validated call in the sandbox and return a truncated result."""
        ...
