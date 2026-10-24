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
- [x] `ToolProvider` adapter: an MCP client that forwards only the tools the
      harness knows and the role is allowed to use, sends the arguments the
      domain validated, truncates results, and turns a transport failure into a
      red result ([ADR 0012](adr/0012-mcp-is-the-tool-protocol.md)).
- [x] Declared MCP servers: a project may name servers of its own in
      `harness.toml`, import their tools by name, and grant them to named
      roles. Tools arrive namespaced (`<server>__<tool>`), arguments are checked
      against the schema the server advertised, and `nih tools` starts every
      declared server so one that would fail during a run fails before one
      ([ADR 0019](adr/0019-declared-mcp-servers.md)).
- [x] Language servers in the sandbox: `find_symbol`, `find_references` and
      `file_outline`, addressed by name so a model never has to count columns.
      One resident server per language, started on demand, with the table of
      who serves what in the domain beside the gate matrix. Verified against a
      real `clangd` - which is how two things got fixed: a cold index answers
      nothing, so the files that mention a name are asked directly, and a miss
      costs a second rather than a whole warm-up
      ([ADR 0020](adr/0020-language-servers-in-the-sandbox.md)).
- [ ] A `diagnostics` tool. The client already collects `publishDiagnostics`;
      nothing exposes them, because "is this code good" is the gate's question
      and deciding when a diagnostics stream has settled is its own problem.
- [ ] Language servers for Rust and Go in the image: both come from toolchain
      tarballs rather than distribution packages, so the wheelhouse recipe does
      not cover them yet.
- [x] Sandbox image: `containers/Containerfile.sandbox`, layered toolchains,
      built from a wheelhouse pinned to the image's own interpreter, and
      published by CI to `ghcr.io/fpgartktic/niharness-sandbox`. A benchmark
      pass runs inside it - tool server, gate and language servers in the
      container, `--network=none` - and scores what the host run scores.
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
- [x] The tool **server**: `niharness.toolserver`, an MCP server that runs
      inside the sandbox, resolves every path against the worktree root (which
      is what catches a symlink), and holds the gate - `git_commit` runs the
      quick tier and refuses with the failing output on red.
- [x] Building the sandbox **image** with that server installed, and running a
      benchmark pass inside it rather than only on the host. Five things had to
      be fixed before it worked at all, and the last was the one that matters:
      an image that can run the harness is not an image that can run the
      *gate*, which needs the project's own linters too.
- [x] `nih implement`: loads the approved plan, creates the run worktree, opens
      the event log, starts the tool server in the sandbox (or on this host with
      `--unsafe-local-tools`, which the event stream records), and runs one
      implementing step per task, stopping at the first task that does not
      finish.
- [x] The review and fix stages: a task runs implement, then the commit gate
      (observed - a commit exists if and only if the gate passed), then review,
      with fixes bounded by `max_revisions`. A step now declares which artifact
      ends it ([ADR 0014](adr/0014-review-starts-from-the-diff.md)).
- [ ] The full gate tier in a run. A task the reviewer acknowledges ends with
      `full_gate_pending`; `nih gate --tier full` has to be run by hand.
- [x] Structured output: a JSON Schema for every artifact, exported under
      `schemas/`, sent as `response_format: json_schema`, with GBNF added where
      the backend advertises it. A live run found the one case that does not
      compose - a schema and a tool grammar in the same request, which
      llama.cpp refuses outright - so the artifact's schema is applied on the
      finalising turn only.
- [x] Forced finalisation, at `finalization_margin` calls before the budget
      ends: `must_finalize` makes the next model call with
      `tool_choice: none`, so a step ends with a written state rather than a
      cut-off tool call.
- [x] The planning stage: `nih plan SPEC.md` validates the spec offline, makes
      a worktree, runs the planner with read-only tools, and writes `PLAN.md`.
      What the planner writes is a `PlanDraft`; the harness binds it to the
      spec's digest, because that field is the one the model could only copy
      ([ADR 0026](adr/0026-the-planner-writes-the-work-the-harness-binds-the-spec.md)).
- [x] `nih plan approve`: approval recorded by digest, which is what
      `nih implement` then runs. The decision stays a human one.
- [ ] The reviewer half of the spec gate - one ACK/NAK pass over `SPEC.md`
      before planning starts. The schema half runs today and is not a
      formality: an acceptance criterion whose verification only restates an
      intention is rejected offline. `spec.accepted` carries `reviewed: false`
      until the other half exists, rather than implying a review nobody ran.
- [ ] `nih run`: plan and implement without stopping for approval. Both halves
      exist now; what is missing is the chaining and a decision about what
      `--json` should say when one command produces two runs.

## What the live runs showed, and what is still open

The harness runs end to end against a real model: plan, worktree, tool loop,
the edit, the formatter, the gate, the detectors, and either a commit or a
handoff with diagnostics.

It has now reached a **green commit**, unattended, on a 4B: retrieval to read
a value out of an ingested datasheet, `replace_in_file` to write it,
`format_code` when the linter objected, and the gate green on `ruff format`,
`ruff check`, `mypy --strict` and `pytest`. Verified by re-running the gate on
the commit by hand.

That is one task, one constant, and a sample of three runs. It says the
pipeline closes; it does not say how often, on what size of task, or for which
models. Those are L1 to L3 questions. L1 is now measured - twenty tasks,
repeated passes, scored by folding each task's event log - and the records are
committed under `benchmarks/results/`. L2 and L3 have no runner, so what this
page says about review quality and about whole tasks is still observation
rather than measurement.

Every earlier run was lost to the same thing: the model knew the change and
could not express it as a unified diff. That is what `replace_in_file`
addresses ([ADR 0022](adr/0022-an-edit-located-by-its-own-text.md)) - and
across the three runs since, the model reached for it first every time and
never attempted a diff.

- [ ] A worked diff example in the implementer's prompt. Three runs were lost
      to diff paperwork, and most of it is now repaired by the tool server -
      but showing the format once would be cheaper than repairing it. This is a
      prompt change, so §11 wants an L1 run in the same change. That run is now
      possible, which is why this item is still open rather than quietly done:
      nobody has taken the before-and-after measurement, and a prompt change
      without a number is how prompts quietly rot.
- [ ] `nih bench --layer L0`. The numbers in
      `benchmarks/results/l0-bonsai2-2026-10-22.jsonl` were taken by hand with
      `llama-bench`; the runner has to produce them, and the record format
      there is a first draft of what it should write.
- [ ] A second slot is a waste for this client. Measured: `--parallel 1`
      raised prompt processing from 4.8 to 15.8 tok/s, because the harness is
      one conversation per run and four slots split the KV cache four ways.
      `nih init` should write the server command line it expects, rather than
      leaving it to be rediscovered.

## Stage: retrieval

- [x] Reading documents: `nih ingest` reads a PDF's text layer where it has
      one and OCRs the pages where it does not, and turns the result into
      chunks that cite their pages (`manual.pdf pp. 12-13`, marked when the
      text came off a rendering). Verified against a typeset PDF and the same
      document rasterised into an image-only scan. `nih ingest` indexes the
      chunks and also writes them to `.nih/corpus/`, so a retrieval result can
      be checked against the text that produced it.
- [x] `VectorStore` adapter (Qdrant over REST) and the `Embedder` port over
      `/v1/embeddings`. No new dependency: five endpoints with the `httpx` the
      inference port already uses. Verified end to end against Qdrant 1.19 and
      bge-m3 - including an English query retrieving a Polish OCR'd page,
      which is what that model is for.
- [x] `rag_query`, served by the harness process rather than the sandbox,
      because the container has no network and the store is a service on the
      host ([ADR 0021](adr/0021-retrieval-runs-in-the-harness.md)). Searches
      the code index, the documents and the run's own context; reports a
      collection nothing has written to rather than counting it as searched.
- [ ] tree-sitter chunking of the project's code, re-indexed by file hash.
      This is the `code` collection's writer; until it exists `rag_query`
      reports that collection as not indexed.
- [x] Compacted run context indexed under the run's collection, recoverable
      with `rag_query`. What leaves the window is chunked - content-derived
      ids, so a retried step writes the same material once - and written to
      `run-<id>`, which is why that collection is in retrieval's default
      search set. A store that is down costs the model its recall and not the
      step: the window is already compacted by then, and
      `context.compacted` reports `indexed_chunks: 0`.
- [ ] A vision tier for a page OCR cannot read - a schematic, a timing
      diagram, a table whose structure is the information. The per-page policy
      in `domain/documents.py` is where a third tier goes.
- [ ] `nih research`.

## Stage: context management end to end

- [ ] `Tokenizer`-backed window measurement in the run loop.
- [ ] The utility-model `ContextSummary` pass, golden-tested.
- [ ] `nih compact`.

## Stage: the TUI

- [x] Follow a live run: `nih-tui --run .nih/runs/<id>` tails the event log by
      byte offset and applies whole lines only
      ([ADR 0017](adr/0017-the-tui-follows-the-event-log.md)).
- [x] Work, Metrics, Bench and Log views.
- [x] Cancel from the viewer: `c` writes the same inbox line `nih cancel` and an
      agent write.
- [ ] Questions picker and plan approval from the viewer.
- [ ] A socket feed, for a viewer that cannot see the run directory - another
      machine, or a container without it mounted.

## Stage: management

- [x] A control plane every driver shares: commands as requests in
      `control.jsonl`, turned into events by the run
      ([ADR 0015](adr/0015-control-actions-are-requests.md)).
- [x] `--json` on the commands that print, `nih runs`, `nih cancel`,
      `nih implement --detach`.
- [x] `nih mcp`: the management surface for an agent - read and steer, with no
      path to the code ([ADR 0016](adr/0016-the-management-surface.md)).
- [x] `nih resume <run-id>` and `run_resume` over MCP: a run records what it
      concluded about each task (`task.finished`), so a continuation skips what
      is already committed and refuses what it cannot know
      ([ADR 0018](adr/0018-a-run-records-its-conclusions.md)).
- [ ] Questions: the loop has the pause and the inbox carries the answers, but
      no stage emits `questions.asked` yet.

## Stage: benchmarks

- [x] `nih bench` runner and the JSONL record format, for L1. Each task runs in
      its own copy of the suite's fixture, through the same loop that runs real
      work, and is scored by folding its events - so the grader is a pure
      function tested without a model.
- [x] The twenty L1 tasks the methodology asks for. Writing them found several
      expectations that measured something other than what they claimed - a
      rule meant to catch a model writing where it should not was failing one
      for *reading* a test, and a sequence expectation scored the route taken
      rather than the state reached - so every task has been through a model at
      least once. The fixture's baseline is an invariant of the suite: format
      clean, lint clean, exactly one deliberate test failure.
- [ ] L2 and L3 runners. L2 needs a golden set with human verdicts to score
      reviewer precision against; L3 needs the vendored reference sets.
- [ ] Answer the ternary question with it. A natively ternary 27B-class model at
      4-6 GB would change the profile arithmetic entirely - see
      `docs/operations/models.md` - and nothing but an L0-L3 run on this
      project's own suites settles whether it holds up at 64k and on a diff
      that has to apply.
- [ ] L0 inference, L1 tool calling, L2 steps, L3 end-to-end.
- [ ] Vendored reference sets (SWE-bench subset, BFCL) with their licenses.

## Housekeeping, not blocking

- [ ] Self-hosted Inter and JetBrains Mono under `docs/assets/fonts/`; the docs
      build currently falls back to system fonts.
- [ ] An asciinema recording of the TUI on the landing page.
- [ ] `mike` deployment of versioned documentation.
