# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""The MCP tool adapter."""

from niharness.adapters.mcp.provider import McpToolProvider, connect_stdio

__all__ = ["McpToolProvider", "connect_stdio"]
