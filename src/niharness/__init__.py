# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""NIHarness: an air-gapped, event-sourced multi-agent coding harness.

The package is layered (see `docs/adr/0002-hexagonal-architecture.md`):

* `niharness.domain` - pure pipeline, contracts, gate logic, loop detectors.
* `niharness.ports` - Protocols the outside world must satisfy.
* `niharness.adapters` - implementations of those Protocols.
* `niharness.cli` - Typer entry points that wire adapters into the domain.
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
