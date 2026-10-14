# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""Typer commands.

The CLI is the only place where adapters are chosen and handed to the domain.
Commands are thin synchronous wrappers over the async core; they parse
arguments, wire ports, and print. No decision is made here.
"""

from niharness.cli.main import app, main

__all__ = ["app", "main"]
