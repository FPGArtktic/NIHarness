# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""The MCP tool adapter: the harness's own tool server, and declared ones."""

from niharness.adapters.mcp.external import (
    CompositeToolProvider,
    ExternalServer,
    ExternalTools,
    ExternalToolsError,
    ServerUnavailableError,
    ToolNotOfferedError,
    attach,
    connect_tools,
    unwrap_group,
)
from niharness.adapters.mcp.provider import McpToolProvider, connect_stdio

__all__ = [
    "CompositeToolProvider",
    "ExternalServer",
    "ExternalTools",
    "ExternalToolsError",
    "McpToolProvider",
    "ServerUnavailableError",
    "ToolNotOfferedError",
    "attach",
    "connect_stdio",
    "connect_tools",
    "unwrap_group",
]
