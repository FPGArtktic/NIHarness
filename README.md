<!--
SPDX-FileCopyrightText: 2026 Mateusz Okulanis
SPDX-License-Identifier: GPL-3.0-or-later
-->

# NIHarness

> [!WARNING]
> **Research and development. Nothing here is ready to depend on.**
>
> This is a working notebook with a quality gate, not a product. Specifically, and
> because "under development" is too vague to be useful:
>
> - **The sandbox is young.** The image builds, and the tool server and the language
>   servers start inside it under `--network=none`. But every number in
>   `benchmarks/results/` so far was taken with `--unsafe-local-tools`, which runs the
>   model's tools on the host, and the container's hardening - read-only root, no
>   capabilities, one writable mount - is asserted by tests over its argument vector
>   rather than by having withstood anything. Treat it as a reasonable default, not as
>   a boundary you have audited.
> - **One of three entry points works.** You can bring a finished `PLAN.md` and
>   execute it. `nih brainstorm` and `nih plan spec` are stubs that exit 69 and say so.
> - **The central question is unanswered.** L1 (tool calling) has numbers as of
>   today; L2 and L3 have a methodology and no runner. One model family, one host.
> - **Nothing is stable.** Event schemas, the config, the artifact contracts and the
>   tool surface all change without notice, and there has been no release.
>
> What *is* true is in [`docs/orientation.md`](docs/orientation.md); what is not is in
> [`docs/roadmap.md`](docs/roadmap.md). The reasoning behind every decision is in
> [`docs/adr/`](docs/adr/). Issues and questions are welcome; promises are not made.

**NIHarness** (*Not Invented Here* harness) is a local, air-gapped, multi-agent coding
harness. Sequential agent roles run on local models (`llama-server`, `bitnet.cpp`),
execute their tools inside a hardened Podman sandbox, and never commit code that does
not pass the quality gate.

Python core, Rust TUI as a thin client. Package `niharness`, CLI `nih`.

## Why

- **Air-gapped by default, and exportable.** The harness itself never leaves
  `localhost` - inference, embeddings and the vector store are local services and a
  non-loopback URL is refused at construction. The sandbox its tools run in gets
  `--network=none` unless a project says otherwise, because an air-gapped network is
  what this is built to be exported to: Podman images and a wheelhouse, no pulls, no
  downloads. Where there *is* internet, `[sandbox] network = "private"` lets the tools
  use it - still without reach to this host's loopback, and recorded in the run
  ([ADR 0023](docs/adr/0023-the-sandbox-network-is-a-deployment-choice.md)).
- **Gated.** `git_commit` is not a convenience wrapper, it is the gate: format, lint,
  incremental build, quick tests. Red output means no commit, for agents and humans.
- **Local models.** One resident coder model per run, a ternary utility model on CPU for
  summaries and compaction. Roles differ by prompt and permission, not by weights.
- **Event-sourced.** Run state is an append-only `events.jsonl` plus snapshots. The TUI
  renders that stream and computes nothing of its own.

## The question this exists to answer

> **How well do 2B-30B models do agentic coding, measured rigorously - and how much
> of the gap to a frontier model can the harness close rather than the weights?**

Nobody has published that with a method you can re-run. Public coding benchmarks
measure a model in isolation, or a frontier model behind a scaffold that is free to
retry, fetch and ask. Neither tells you what happens when the ceiling is a 9B on a
laptop GPU, the sandbox is sealed, and a red gate means the commit does not
happen. Almost nobody runs a 4B agentically, because almost nobody has to - unless
the work is air-gapped, and then there is no other option.

### The hypothesis

**Small models fail at protocol, not at comprehension - and protocol failures are the
harness's to fix.**

Every harness fix so far came from watching a weak model fail and then making the
*harness* explain itself better, never from making the prompt longer. One run is the
whole thesis in miniature: asked for a register's supply voltage, a 4B called
`rag_query` first, read the value out of an ingested datasheet whose erratum
contradicted the obvious answer, and wrote `VDDC_VOLTS = 1.20` with a comment naming
the revision. It then failed three times in a row to express that one-line change as
a unified diff.

Finding the value was never the problem. Saying it in the required form was. If that
generalises, the interesting engineering is not in the weights.

### Why loop interventions per task

A model that lands a correct patch after sixty redundant calls is not equivalent to
one that lands it in eight, and `pass@1` cannot tell them apart. The
[intervention ladder](docs/concepts/tool-loops.md) makes every pathology - an exact
repeat, a worktree state cycle, a progress stall, gate thrash - an event, so "how hard
did the harness have to work" becomes a number per task and therefore a
model-selection criterion. On this hardware it is also the *cost* metric: a redundant
call on a CPU-only profile is tens of seconds of wall clock.

### What is measured so far

**L0 only** - throughput, and by hand rather than by the runner. Host: Intel i5-12500H
(4 P-cores + 8 E-cores), RTX 3050 Laptop 4 GiB, 62 GiB RAM. Ternary Bonsai 2 on the
PrismML llama.cpp fork, prompt-processing / generation tok/s:

| Model | Packing | CPU (8 threads) | CUDA |
|---|---|---|---|
| 4B | PQ2_0 | 53.7 / 15.8 | **1539 / 105.6** |
| 9B | PQ2_0 | 19.4 / 8.7 | **1035 / 59.0** |
| 27B | PQ2_0 | - | 130.6 / 3.2 *(partial offload: 6.7 GiB does not fit in 4)* |
| 27B | PTQ1_0 | 2.1 / 1.7 | - |

Two findings worth the table on their own. The packing dominates the parameter count:
the 27B in `PTQ1_0` is the *smaller* file (5.5 GiB against 6.7) and processes a prompt
on a CPU **25x slower** than the 4B in `PQ2_0`, for 6.4x the parameters. Picking by
file size picks the wrong one. And more threads than P-cores costs throughput - 16
threads is slower than 8, because the E-cores contend.

Records: [`benchmarks/results/`](benchmarks/results/), one JSONL line each, tagged with
the harness commit, model, quantization, profile, host and date.

### What is *not* measured

Everything that would actually answer the question. L1 (tool calling), L2 (gate and
review) and L3 (end to end) have a [methodology](docs/benchmarks/methodology.md), a
record format, and a seed set of tasks - 5 of L1's 20, 3 of L2's golden cases, 2 of
L3's - and **no runner to execute any of them**, so no data. Until there is, every
claim here about model behaviour is an observation from roughly ten live runs, which
is an anecdote and not a measurement, and is labelled as one wherever it appears.

One result is worth stating against that, with its limits. A **4B** has now produced
a green commit unattended: `rag_query` to read a value out of an ingested datasheet
whose erratum contradicted the obvious answer, `replace_in_file` to write it,
`format_code` when the linter objected, then the gate - `ruff format`, `ruff check`,
`mypy --strict`, `pytest` - green, and a commit whose body cites the erratum and what
ignoring it destroys. Verified by re-running the gate on the commit by hand.

The limits matter as much as the result: one task, one file, one constant, and a
sample of three runs of which two finished inside the time limit and both committed.
It says the pipeline closes. It does not say how often, on what size of task, or for
which models - which is what L1 to L3 are for, and they have no runner yet.

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

## What works, and what is missing

The pipeline executes an approved plan end to end against a real model - worktree,
tool loop, edits, formatter, tests, commit attempt, gate, loop detectors, and either a
commit or a handoff with diagnostics. A 4B has taken that path to a green commit
unattended. Everything below is the detail of that sentence.

| Subsystem | Works | Missing |
|---|---|---|
| **Domain core** | contracts, event catalogue, tool-call validation with path confinement, role allowlists, budgets with forced finalisation, two-tier gate with failure signatures, loop detectors and the intervention ladder, compaction policy, the fold that derives run state | - |
| **Tools** | `read_file`, `list_tree`, `grep`, `replace_in_file` (creates too), `apply_patch` with its clerical repairs, `format_code`, `run_lint`/`run_build`/`run_tests`, `git_commit` as the gate, `rag_query`, the LSP symbol tools | `diagnostics`; `add_dependency` cannot consult a real index |
| **Sandbox** | the Containerfile, the hardened argument vector, two network modes, `--unsafe-local-tools` as the escape hatch | **the image has never been built**, so none of it has confined a real task; no wheelhouse; Quadlet units not rendered |
| **Run loop** | `nih implement`, review and fix stages, structured output with GBNF, window building and compaction, resume, cancel | the full gate tier inside a run; a producer for `questions.asked` |
| **Pipeline entries** | **C**: bring a `PLAN.md` and execute it | **A** (`nih brainstorm`) and **B** (`nih plan spec`) are stubs, so `nih run` is too |
| **Retrieval** | Qdrant over REST, embeddings over `/v1/embeddings`, `nih ingest` reading PDFs with OCR and citing pages, `rag_query` over documents | the code index (needs tree-sitter chunking); compacted context is not indexed, so `nih compact` and `nih research` are stubs |
| **Management** | `nih mcp`, `nih runs`, `nih status`, `nih cancel`, `nih resume`, `--json`, `--detach` | answering `Questions` from outside |
| **TUI** | follows a live run's event log, writes the control inbox, cancels | the questions picker and plan approval |
| **Benchmarks** | `nih bench --layer L1`, scored by folding the event log, `--repeat` for variance; L0 numbers taken by hand | **L2 and L3 have no runner**; 14 of L1's 20 tasks; no vendored reference sets |

Three things block the rest, in order: **the sandbox image** (the safety story is
unexercised), **L2 and L3** (review quality and end-to-end are unmeasured), and **the
plan stage** (two of three entry points). Roughly 24 of 59 roadmap items are done; the
count is a poor measure, because the remaining ones are the large ones.

[`docs/orientation.md`](docs/orientation.md) is the long version of this table,
[`docs/roadmap.md`](docs/roadmap.md) the itemised list, and
[`docs/adr/`](docs/adr/) says why anything is shaped the way it is.

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
uv run nih implement --detach # run it in the background
uv run nih runs               # what is running, and what it has done
uv run nih status <run-id>    # fold the event log into a snapshot
uv run nih cancel <run-id>    # stop at the next step boundary
uv run nih mcp                # the management surface, for an agent to drive
```

The harness is meant to be *managed*, not watched: an agent writes the spec and
the plan, starts the run and reads digests, while the local models do the
reading, patching and testing. See
[docs/operations/managing-from-claude.md](docs/operations/managing-from-claude.md).

## Layout

| Path | What lives there |
|---|---|
| `src/niharness/domain/` | pure pipeline, contracts, gate logic, loop detectors, compaction |
| `src/niharness/ports/` | Protocols the outside world must satisfy |
| `src/niharness/adapters/` | HTTP inference and embeddings, MCP, Qdrant, PDF, git, Podman, JSONL |
| `src/niharness/application/` | the step executor, the window, compaction, the plan runner |
| `src/niharness/management/` | the run registry and the `nih mcp` surface |
| `src/niharness/toolserver/` | the sandbox end of the protocol: the gate, and the LSP client |
| `src/niharness/cli/` | Typer commands |
| `src/niharness/prompts/` | versioned prompt layers |
| `schemas/` | generated JSON Schema: the contract with the TUI |
| `tui/` | Rust crate (ratatui), follows the run's event log |
| `benchmarks/` | L0-L3 suites and the committed JSONL records |
| `docs/` | MkDocs sources, including `docs/adr/` |

## Documentation

`uv run mkdocs serve`, or the published site once Read the Docs is wired up.

## License

GPL-3.0-or-later. Everything in the repository — Python, Rust, prompts, Containerfiles,
Quadlet templates, documentation, benchmark suites — is under the same license, and the
tree is [REUSE](https://reuse.software) compliant. See `LICENSE` and `THIRD_PARTY.md`.
