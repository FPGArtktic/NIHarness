<!--
SPDX-FileCopyrightText: 2026 Mateusz Okulanis
SPDX-License-Identifier: GPL-3.0-or-later
-->

# NIHarness

**NIHarness** (*Not Invented Here* harness) is a local, air-gapped, multi-agent coding
harness. Sequential agent roles run on local models (`llama-server`, `bitnet.cpp`),
execute their tools inside a Podman sandbox with no network, and never commit code that
does not pass the quality gate.

Python core, Rust TUI as a thin client. Package `niharness`, CLI `nih`.

## Why

- **Air-gapped.** No network at runtime: no package installs, no model downloads, no
  HTTP outside `localhost` in any code path.
- **Gated.** `git_commit` is not a convenience wrapper, it is the gate: format, lint,
  incremental build, quick tests. Red output means no commit, for agents and humans.
- **Local models.** One resident coder model per run, a ternary utility model on CPU for
  summaries and compaction. Roles differ by prompt and permission, not by weights.
- **Event-sourced.** Run state is an append-only `events.jsonl` plus snapshots. The TUI
  renders that stream and computes nothing of its own.

## Pipeline

```
brainstorm ─┐
SPEC.md ────┼─→ spec gate ─→ plan ─→ plan gate ─→ [approval] ─→ implement
PLAN.md ────┘                                                      │
                                                                   ▼
                      full gate ←─ fix ←─ review ←─ git_commit (GATE)
```

Three entry points, one pipeline: brainstorm with a model, bring your own `SPEC.md`, or
bring a finished plan and only execute it. Every entry passes the same gates. Each stage
boundary starts a **fresh run** seeded only with the artifact, so what matters has to be
written down, not remembered.

## Status

Early. The domain core (contracts, gate planning, tool validation, loop detectors,
compaction policy, event fold) and the event-log adapter are in place; the sandbox,
MCP and inference adapters are being filled in. See `CHANGELOG.md` and
`docs/adr/` for what has been decided and why.

## Quickstart (development)

```bash
uv sync --all-groups          # install from the lockfile
uv run ruff format . && uv run ruff check .
uv run mypy --strict src
uv run pytest                 # unit + contract; integration needs Podman
uv run lint-imports           # architecture boundaries
uv run reuse lint             # SPDX / REUSE compliance
```

Then:

```bash
uv run nih init               # probe hardware, pick a profile, run the L0 benchmark
uv run nih brainstorm         # entry A: interactive session → SPEC.md
uv run nih plan SPEC.md       # entry B: spec gate + planning → approval
uv run nih plan import plan.md # entry C: external plan → Plan schema
uv run nih implement          # execute the approved plan
uv run nih status <run-id>    # fold the event log into a snapshot
```

## Layout

| Path | What lives there |
|---|---|
| `src/niharness/domain/` | pure pipeline, contracts, gate logic, loop detectors, compaction |
| `src/niharness/ports/` | Protocols the outside world must satisfy |
| `src/niharness/adapters/` | OpenAI-compatible HTTP, MCP, Qdrant, git, Podman, JSONL events |
| `src/niharness/cli/` | Typer commands |
| `src/niharness/prompts/` | versioned prompt layers |
| `schemas/` | generated JSON Schema: the contract with the TUI |
| `tui/` | Rust crate (ratatui), reads the run socket |
| `docs/` | MkDocs sources, including `docs/adr/` |

## Documentation

`uv run mkdocs serve`, or the published site once Read the Docs is wired up.

## License

GPL-3.0-or-later. Everything in the repository — Python, Rust, prompts, Containerfiles,
Quadlet templates, documentation, benchmark suites — is under the same license, and the
tree is [REUSE](https://reuse.software) compliant. See `LICENSE` and `THIRD_PARTY.md`.
