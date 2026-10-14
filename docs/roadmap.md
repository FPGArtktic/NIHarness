<!--
SPDX-FileCopyrightText: 2026 Mateusz Okulanis
SPDX-License-Identifier: GPL-3.0-or-later
-->

# Roadmap

What works, what does not, and in which order the gaps close. Commands that are
not wired up exit with code 69 (`EX_UNAVAILABLE`) and name what they need; this
page is the "tracked in" they point at.

## Works today

| Command | What it does |
|---|---|
| `nih init` | probes CPU, RAM and VRAM, picks a hardware profile, writes `harness.toml` |
| `nih config` | prints the merged configuration (defaults, profile, project file, `NIH_` environment) |
| `nih gate` | prints the gate plan for this repository, per tier, without a container |
| `nih status` | folds a run's `events.jsonl` into a snapshot, or lists runs |
| `nih plan check` | validates `PLAN.md` against the `Plan` schema - the plan gate, offline |
| `nih plan import` | normalises an external plan that already conforms |
| `nih docs generate` | regenerates `schemas/` and the generated reference pages |

## Stage: tools and sandbox

The first thing a model needs is hands.

- [ ] `Sandbox` adapter: rootless Podman, `--network=none`, project bind-mount,
      Quadlet units generated from `quadlet/`.
- [ ] `ToolProvider` adapter: MCP client, tool schemas from
      `niharness.domain.tools`, results truncated and digested.
- [ ] Sandbox image: `containers/Containerfile.sandbox`, layered toolchains.
- [ ] Integration tests behind the `integration` marker.

## Stage: the run loop

- [ ] `ToolLoop` in the domain: build window, call model, validate call,
      execute, append events, run detectors, check stop conditions.
- [ ] Structured output: JSON Schema for every artifact, GBNF where the backend
      supports it.
- [ ] Forced finalisation two calls before the budget ends.
- [ ] `nih implement`, `nih run`, `nih resume`.

## Stage: retrieval

- [ ] `VectorStore` adapter (Qdrant) and the embedding backends.
- [ ] tree-sitter chunking, re-index by file hash.
- [ ] Compacted run context indexed under the run's collection, recoverable
      with `rag_query`.
- [ ] `nih research`.

## Stage: context management end to end

- [ ] `Tokenizer`-backed window measurement in the run loop.
- [ ] The utility-model `ContextSummary` pass, golden-tested.
- [ ] `nih compact`.

## Stage: the TUI

- [ ] Unix socket event feed from the harness.
- [ ] Work, Metrics, Bench and Log views.
- [ ] Questions picker, cancel on `Esc`.

## Stage: benchmarks

- [ ] `nih bench` runner and the JSONL record format.
- [ ] L0 inference, L1 tool calling, L2 steps, L3 end-to-end.
- [ ] Vendored reference sets (SWE-bench subset, BFCL) with their licenses.

## Housekeeping, not blocking

- [ ] Self-hosted Inter and JetBrains Mono under `docs/assets/fonts/`; the docs
      build currently falls back to system fonts.
- [ ] An asciinema recording of the TUI on the landing page.
- [ ] `mike` deployment of versioned documentation.
