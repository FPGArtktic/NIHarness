# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""Managing runs: the registry, the plan store, and the views over both.

This layer is what a *driver* uses - a person at the CLI, the TUI, or an agent
over MCP. It starts runs, stops them, approves plans and renders bounded
answers about what is happening. It never touches project source, never
commits, and has no path to the gate: the work belongs to the models inside the
sandbox, and the gate belongs to the tool server.

Everything here is driver-agnostic on purpose. `niharness.cli` is one caller,
`niharness.management.server` is another, and neither knows about the other.
"""

from niharness.management.plans import PlanStore
from niharness.management.runs import RunEntry, RunRegistry

__all__ = ["PlanStore", "RunEntry", "RunRegistry"]
