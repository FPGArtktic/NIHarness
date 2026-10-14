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

- [x] `Sandbox` adapter: rootless Podman, one short-lived container per
      command, `--network=none`, read-only root, all capabilities dropped,
      `--userns=keep-id`, CPU, memory and pid caps, and a container timeout with
      a grace period. The argument vector is a pure function asserted by a
      contract test; see [ADR 0008](adr/0008-sandbox-is-one-container-per-command.md).
- [ ] Quadlet units rendered from `quadlet/` by `nih init`.
- [ ] `ToolProvider` adapter: MCP client, tool schemas from
      `niharness.domain.tools`, results truncated and digested.
- [ ] Sandbox image: `containers/Containerfile.sandbox`, layered toolchains.
- [x] Integration tests behind the `integration` marker: run them with
      `NIH_SANDBOX_IMAGE=<image> uv run pytest -m integration`. They assert the
      properties that only a real container can show - the worktree is
      writable, the root filesystem is not, there are no network routes, a red
      command stays red, a hanging command is stopped.

## Stage: output reduction (optional, host-dependent)

Tool results are the bulk of a run's token spend: file reads, directory
listings, build logs, test output. The harness already truncates and digests
them, but a local filter can cut what reaches the window much further before
truncation ever applies.

[RTK](https://github.com/rtk-ai/rtk) is a local Rust CLI proxy that does
exactly that - `rtk read`, `rtk grep`, `rtk tree`, `rtk find`, `rtk git`,
`rtk diff`, `rtk err`, `rtk test`, `rtk ctest`, `rtk lint`, `rtk format`,
`rtk log` - and its subcommands map almost one to one onto this harness's tool
set and gate matrix. It is a single local binary with no network use, so it
fits the air gap. Measured on one developer machine it removed about 40% of the
tokens across several thousand commands; whether that holds for *this* harness
is a benchmark question, not a claim.

- [ ] Detect it rather than assume it: probe inside the **sandbox image**, not
      on the host, because tools never run on the host. Probe by behaviour
      (`rtk gain` must succeed), not by name: an unrelated tool ships the same
      `rtk` command, and running the wrong one must be a clean fallback to the
      raw command, not a failure.
- [ ] Config under `[tools.rtk]`: `mode = "auto" | "off" | "required"` plus a
      per-tool map, so a project can filter reads and listings but keep build
      output raw. Default `auto`: use it when present, work identically when
      not.
- [ ] Point its local tracking store inside the container, or disable tracking
      outright. It keeps a usage history by default, and no tool may write
      outside the run worktree - that rule has no exception for a tool that
      only writes statistics.
- [ ] Decide, in an ADR, who executes the command. Either the harness runs the
      raw command and filters the captured output, or it invokes `rtk <tool>`
      and trusts it to forward the exit code. The second is simpler and the
      first keeps the raw bytes; the ADR picks one and states why.

### Invariants any such filter has to respect

These are the reason this is a separate stage with its own ADR rather than a
flag on the sandbox adapter:

1. **A filter may never change a verdict.** The gate decides on the exit code
   of the real command. Red stays red, and no filtering mode may turn missing
   output into a pass.
2. **Signatures and digests come from the raw output.** The
   [failure signature](concepts/gate.md) and the result digest that the
   progress-stall detector compares must be computed before filtering -
   otherwise upgrading the filter silently reclassifies every historical
   failure, and loop detection quietly changes behaviour.
3. **On red, the model gets the unfiltered tail.** Filtering is for bulk reads
   and green output. The one line that explains a failure is exactly the line a
   summariser is most likely to drop.
4. **The validated argv is what runs.** The sandbox adapter must not silently
   rewrite a command the domain validated. A rewrite is part of the plan, is
   visible in `tool.called`, and is replayable - a transparent hook that
   rewrites argv behind the harness's back is the one design that is not
   acceptable here.
5. **Record the tool and its version in the event stream**, and pin that
   version in the sandbox image. A replay must be able to tell "the output
   changed because the code changed" from "because the filter changed".
6. **Token counts still come from the backend tokenizer.** The filter's own
   savings numbers are its estimate of its own worth; the harness measures the
   window with `/tokenize` like everything else.
7. **Truncation still applies afterwards**, in this order: filter, then the
   byte cap, then the digest line.

### How the gain gets proven

- [ ] L1 and L3 runs with `mode = "off"` and `mode = "auto"` on the same
      harness commit, comparing tokens per task, pass@1 and loop incidents. A
      filter that saves tokens and costs a point of pass@1 is a bad trade, and
      only the benchmark can say which happened.
- [ ] Contract tests on recorded output pairs (raw and filtered) asserting the
      invariants above, especially exit-code fidelity and that red output keeps
      its tail.
- [ ] Its license recorded in its ADR note and in `THIRD_PARTY.md`, like any
      other dependency added to the sandbox image.

## Stage: the run loop

- [x] `ToolLoop` in the domain, as a pure state machine: `next_action` says
      what should happen, each `observe_*` folds what came back and returns the
      events to append. Validation, the detector window, the intervention
      ladder, forced finalisation and every stop condition live here, with no
      I/O; see [ADR 0009](adr/0009-the-tool-loop-is-a-pure-state-machine.md).
- [x] The executor that performs those actions, wiring `ModelBackend`,
      `ToolProvider`, `Vcs` and `EventSink` together, in the new
      `niharness.application` layer ([ADR 0010](adr/0010-an-application-layer.md)).
- [x] Window building and compaction inside the executor: the window measures
      itself through the `Tokenizer` port before every model call, applies the
      policy's plan, and emits `context.compacted`. The utility model produces
      the `ContextSummary` when the deterministic passes are not enough.
- [ ] The MCP `ToolProvider` adapter, so the executor has real tools to run
      (it is written against the port, not against MCP).
- [ ] Structured output: JSON Schema for every artifact, GBNF where the backend
      supports it.
- [ ] Forced finalisation two calls before the budget ends.
- [ ] `nih implement`, `nih run`, `nih resume`.

## Stage: retrieval

- [ ] `VectorStore` adapter (Qdrant) and the embedding backends.
- [ ] tree-sitter chunking, re-index by file hash.
- [ ] Compacted run context indexed under the run's collection, recoverable
      with `rag_query`. Until this lands, folded-away turns live only in the
      event log, and the window says so in place of the summary it could not
      index.
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
