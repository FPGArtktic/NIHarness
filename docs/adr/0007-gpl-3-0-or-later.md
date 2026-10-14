<!--
SPDX-FileCopyrightText: 2026 Mateusz Okulanis
SPDX-License-Identifier: GPL-3.0-or-later
-->

# 0007. GPL-3.0-or-later for the whole repository

- Status: Accepted
- Date: 2026-10-14

## Context and problem statement

This harness decides what gets committed to someone's codebase. A user who
depends on that should be able to read and modify the thing that decides, and
anyone shipping a modified version should be under the same obligation.

## Decision drivers

- Modifications that ship should come back as source.
- The licensing must be machine-checkable, not a claim in a README.
- Dependencies must stay compatible, in both ecosystems.

## Considered options

1. **GPL-3.0-or-later for everything in the repository.**
2. Apache-2.0, for maximum adoption.
3. Split licensing: permissive core, copyleft TUI.

## Decision outcome

Option 1, uniformly: Python, Rust, prompts, Containerfiles, Quadlet templates,
documentation and benchmark suites.

Mechanics:

- `LICENSE` holds the verbatim GPLv3 text; `LICENSES/GPL-3.0-or-later.txt` is
  the same text where [REUSE](https://reuse.software) expects it.
<!-- REUSE-IgnoreStart -->
- Every source file carries `SPDX-License-Identifier: GPL-3.0-or-later` and
  `SPDX-FileCopyrightText`. Files that cannot carry a comment are annotated in
  `REUSE.toml`.
<!-- REUSE-IgnoreEnd -->
- `reuse lint` runs in the quick gate; `pip-licenses --allow-only-file
  licenses.toml` and `cargo deny check licenses` run in the full gate.
- Dependencies must be GPLv3-compatible: MIT, BSD, Apache-2.0, LGPL and MPL-2.0
  are; anything else needs checking, and a new dependency states its license in
  its ADR note.
- Vendored third-party material keeps its own license file next to it and is
  listed in `THIRD_PARTY.md`.
- Models are not distributed here; their licenses are recorded in
  [Models](../operations/models.md) and verified at download time.

## Consequences

- Prompts are source, and therefore licensed like source. That is intentional:
  the prompts are a large part of the behaviour.
- A proprietary fork cannot ship without releasing its changes, which rules out
  some commercial adoption. Accepted.
- Adding a dependency with an incompatible license is a gate failure rather than
  a discussion.
