<!--
SPDX-FileCopyrightText: 2026 Mateusz Okulanis
SPDX-License-Identifier: GPL-3.0-or-later
-->

# Getting started

Three pages, in order:

1. [Install](install.md) - the air-gap bundle, the toolchain, what must exist
   before anything runs.
2. [First run](first-run.md) - `nih init`, then the commands that work today.

If you are here to *work on* the harness rather than use it, read
[Orientation](../orientation.md) instead; it is written for that.

!!! warning "Status"
    The pipeline executor is not wired up yet. Commands that need a model, a
    container or a vector store exit with code 69 and say what they need. See
    [the roadmap](../roadmap.md).
