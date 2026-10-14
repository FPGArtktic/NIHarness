# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""Ports: the Protocols the outside world must satisfy.

A port is the only way the domain reaches anything real. Adapters implement
these Protocols; the domain never imports an adapter, and a port never imports
one either. Adding a subsystem means adding a port here first.
"""

from niharness.ports.event_sink import EventSink
from niharness.ports.model_backend import BackendInfo, ModelBackend
from niharness.ports.sandbox import ProcessResult, Sandbox
from niharness.ports.tokenizer import Tokenizer
from niharness.ports.tool_provider import ToolProvider
from niharness.ports.vcs import Commit, Vcs, Worktree
from niharness.ports.vector_store import VectorStore

__all__ = [
    "BackendInfo",
    "Commit",
    "EventSink",
    "ModelBackend",
    "ProcessResult",
    "Sandbox",
    "Tokenizer",
    "ToolProvider",
    "Vcs",
    "VectorStore",
    "Worktree",
]
