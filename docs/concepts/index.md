<!--
SPDX-FileCopyrightText: 2026 Mateusz Okulanis
SPDX-License-Identifier: GPL-3.0-or-later
-->

# Concepts

Seven ideas carry the whole harness. Each page below is the authoritative
description of one of them; the code follows these pages, and when they disagree
the page is a bug.

| Page | The idea |
|---|---|
| [Architecture](architecture.md) | A pure domain, Protocol ports, replaceable adapters, and a check that enforces it |
| [Pipeline](pipeline.md) | Three entry points, one gated pipeline, fresh runs at every stage boundary |
| [Gate](gate.md) | `git_commit` is a gate in two tiers, with a normalised failure signature |
| [Prompts](prompts.md) | Layered, versioned, golden-tested prompts; reasoning logged and never fed back |
| [Context](context.md) | The window is a derived view; compaction is a policy over it |
| [Events](events.md) | Run state is an append-only stream, and everything else is derived |
| [Tool loops](tool-loops.md) | Bounded ReAct loops, pathology detectors, an intervention ladder |
