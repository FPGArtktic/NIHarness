<!--
SPDX-FileCopyrightText: 2026 Mateusz Okulanis
SPDX-License-Identifier: GPL-3.0-or-later
-->

# NIHarness

A local, air-gapped, multi-agent coding harness: sequential agent roles on local
models, tools in a container with no network, and nothing committed that the gate
calls red.

```text
$ nih gate --tier quick
tier: quick
languages: docs, python, rust
13 command(s):
  [format] cargo-fmt          cargo fmt --check
  [format] ruff-format        ruff format --check .
  [lint  ] mypy               mypy --strict src
  [lint  ] reuse              reuse lint
  ...
  [test  ] pytest             pytest
  [docs  ] mkdocs-strict      mkdocs build --strict
```

<div class="grid cards" markdown>

- :material-rocket-launch: **[Getting started](getting-started/index.md)**

    Install the air-gap bundle, run `nih init`, drive a first task.

- :material-sitemap: **[Concepts](concepts/index.md)**

    The pipeline, the gate, prompts, context management, events, tool loops.

- :material-book-open-variant: **[Reference](reference/cli.md)**

    CLI, configuration and the event catalogue, generated from the code.

- :material-robot: **[Orientation](orientation.md)**

    What this project is and how to work on it - written for a new contributor,
    human or agent.

</div>

## Why it is built this way

- **Air-gapped** - no network at runtime. Dependencies come from a local
  wheelhouse, models from local files, and the only HTTP the harness speaks is
  to `localhost`.
- **Gated** - `git_commit` is the gate: format, lint, build, quick tests. Red
  means no commit, for the agent and for you.
- **Local models** - one resident coder model per run plus a ternary utility
  model on CPU. Roles differ by prompt and permission, not by weights.
- **Event-sourced** - run state is an append-only `events.jsonl`. The TUI
  renders that stream; it computes nothing of its own.

## Status

Early, and honest about it: the domain core and three adapters are in place, the
pipeline executor is not. [The roadmap](roadmap.md) says exactly which commands
work today and which exit with `not available yet`.
