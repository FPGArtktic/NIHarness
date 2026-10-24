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
- [x] The reviewer half of the spec gate: one pass, no tools, over `SPEC.md`
      before planning starts, looking for what the schema cannot see - a
      criterion that would hold whether or not the work was done. It answers
      with a `SpecVerdict` rather than a `Verdict`, because an ACK on the
      latter has to stand on a green build and green tests and a spec has
      neither; and a NAK must name what is missing. `spec.accepted` carries
      `reviewed`, so a run with `--no-spec-review` says so.
- [x] `nih run`: plan and implement without stopping for approval. It chains
      the two fresh runs and approves the plan on your behalf - and refuses to
      implement a plan whose `open_questions` are not empty, because the one
      stop this command removes is the human reading of the plan, and a
      planner saying it had to guess is exactly what that stop was for.
      `--ignore-open-questions` is for when you have read them.

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
- [x] `nih bench --layer L0`. Three timed requests per context size, using the
      server's prefix cache as the instrument: the repeat of a single-token
      request costs the per-request overhead and nothing else, so subtracting
      it from the cold request leaves the prompt and from the long one leaves
      the generation. No backend timings are read, because the port is HTTP
      and nothing more. It agrees with the hand-taken `llama-bench` figures to
      1.3% on prompt processing, with a 0.2% spread across passes - and it
      took two wrong versions to get there, the second of which reported
      68,769 tok/s on a laptop GPU because every prompt after the first run
      was served from the cache.
- [ ] Load time, which §11 asks L0 for. It means owning the backend's
      lifecycle, and the harness does not start the server.
- [ ] A second slot is a waste for this client. Measured: `--parallel 1`
      raised prompt processing from 4.8 to 15.8 tok/s, because the harness is
      one conversation per run and four slots split the KV cache four ways.
      `nih init` should write the server command line it expects, rather than
      leaving it to be rediscovered.

## One rung of the ladder is all that has ever run

`exact_repeat` has fired 29 times across three L1 passes, 6 across six L2
passes, and ten to fourteen *per pass* in L3. Two others have now appeared:
`uncommitted_completion` fires in every L3 pass since the implementer's commit
check landed, and `state_cycle` fired **once**, which is the first time any
detector written before this session has fired anywhere other than
`exact_repeat`. `progress_stall`, `gate_thrash`, `invalid_calls`,
`degenerate_output` and `unbacked_claim` remain at **zero** in every recorded
run that tallies them.

- [ ] Find out which reading is true: thresholds in `[loop]` set past what
      these tasks provoke, or tasks that do not provoke them. A case built to
      trigger one detector on purpose would answer it for that detector, and
      is the same shape of case the evidence check needs.
- [ ] Until then, treat the ladder as one rung when reasoning about it. Levels
      2 to 4 - the reflection turn, the rollback, the step failure - are
      reached by `exact_repeat` escalating and by nothing else.

## The open problem both layers found

Two places that have to agree, and only one of them changed. It is the same
failure in the implementer and in the reviewer, measured independently:

- **L1 `two-places-must-agree`** fails every pass. The task says a constant
  and the document that quotes it must match; the model changes the constant
  and commits.
- **L1 `write-down-what-cannot-be-done`** fails every pass again, having
  passed 3/3 at `e62dc3e`. The tool tally says why, which no record could
  before: `add_dependency` was **never called** in any of the three passes -
  the model explored the tree, read files and ran the linter instead. The tool
  is fine; its contract tests pass, and it refuses a dependency absent from
  the mirror by writing the request to `NOTES.md`. Nothing in the changes
  between the two runs plausibly touches the implementer's choice of tool, and
  the `e62dc3e` records predate the tally so they cannot say whether it was
  called then. Unattributed, and recorded as such rather than explained.
- **L2 `documentation-left-behind`** is waved through every pass. The change
  raises a bound in the code and leaves `docs/limits.md` quoting the old one;
  the reviewer ACKs it.

Neither is a tool-use failure, and neither is caught by anything the gate
runs - two files can disagree and still pass format, lint and tests. The
reviewer's own prompt asks about blast radius in so many words, and the
implementer's policy says one logical change; the models do the first half of
the change and stop.

- [ ] Decide whether the harness has a door here. The plan already names the
      files a task expects to touch (`Task.files`, documented as a hint), and
      the forced-finalisation turn is the natural place to say "the plan named
      `docs/limits.md` and nothing has changed it". That is a checklist, which
      is what §6's Definition of Done layer already is - but it is also the
      harness doing part of the thinking, so it needs a measurement either
      side of it rather than an opinion.
- [ ] A detector for it in the reviewer's direction is harder and more
      interesting: "a constant moved and a file that quotes it did not" is a
      question about the whole tree, not about the diff, and `grep` is in
      every role's allowlist already.

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

## Stage: brainstorm, entry A

- [x] `nih brainstorm "<topic>"`: a conversation at a terminal. A session is
      many steps over one window, so each turn is a step the detectors and the
      ladder apply to, and the stage's budget is spent across the session
      rather than per turn. `/spec` asks for a `SpecDraft` on a turn with no
      tools and the harness binds the origin
      ([ADR 0031](adr/0031-a-brainstorm-session-is-many-steps-over-one-window.md)).
- [ ] The TUI pane §9 describes: the running draft, visible as it takes shape.
      The session is a command and the pane is a view of it, which is the right
      way round - the TUI reads the event log and does not type for anybody.
- [ ] A `questions.asked` producer in the session. §9 says the brainstormer
      asks `Questions` and the TUI renders a picker; today it asks in prose,
      which a human can answer and a transcript cannot be scored on.

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
- [x] `nih bench --layer L2`: the review half. Eight golden cases, four of
      each verdict, each one the fixture plus a directory of replacement files
      committed as one change - so a case cannot stop applying and can be read
      rather than reconstructed from hunks. Every case is green, because the
      gate runs before review and a defect the tests catch never reaches a
      reviewer. A NAK is scored on where and why, and a verdict claiming the
      tests passed has to point at a green `run_tests` call in its own step
      ([ADR 0027](adr/0027-a-review-case-is-a-tree-and-its-verdict-must-be-earned.md)).
- [x] Record **which pathology** fired, per case and per task. Both suites
      counted interventions and kept the highest level reached, and neither
      said what caused them - so a change to the intervention ladder could not
      be measured, which three L2 passes demonstrated by being consistent with
      the evidence check firing and with its never firing once.
- [x] `nih bench report`, and the results page generated by the same
      function. A column is a harness commit, a cell is the range across its
      passes, and a second table says how many of a column's passes each case
      or task passed - which is the shape of every comparison made so far,
      previously assembled by hand.
- [x] Draw each metric that varies, as well as tabulating it: a bar from a
      column's low to its high with a dot per pass, hand-written SVG with no
      new dependency and no colour of its own, so one drawing serves both
      palettes. A ratio against 0 to 1, anything else against its own
      labelled ends.
- [ ] The rest of what §12 asks of the benchmark pages: grouped bars across
      model x quantization x profile, and a regression table against the
      previous harness commit. The strips show one metric's spread over time,
      which is the comparison that has been needed so far; those two compare
      across configurations, and there is one configuration to compare.
- [ ] Check the inverse claim too. The harness verifies "the tests passed"
      against a green call in the same step; "the tests could not be run" is
      verifiable against the absence of one, and a reviewer refused a correct
      change on exactly that false ground in two of six L2 passes - while
      other cases in those same passes ran the tests green, which the ACKs
      and `evidence_ratio` 1.0 together prove. Needs a case that provokes it,
      because two occurrences are a pattern and not a number.
- [x] Tighten L2's `mentions_any` to the tokens that discriminate. Six
      passes put `reasoned_ratio` at 1.0 throughout, which means it
      distinguished nothing: `LOWER_BOUND`, `except` and `ValueError` are on
      lines their cases change, so quoting the diff scored. One rule now
      governs the key - no token may appear in the case's own diff - and a
      test checks it against the case files rather than leaving it to be
      remembered. It waited for the evidence check's before-and-after to
      conclude, because moving an answer key makes two records incomparable.
- [ ] The implementer half of L2 that §11 asks for: first-attempt gate pass
      rate and revisions per task over a golden set of tasks. That needs
      implement, gate, review and fix run end to end, which is closer to L3's
      shape than to the review half's.
- [x] `nih bench --layer L3`: whole tasks, end to end, graded by tests the
      model never saw. The real pipeline - `ImplementationRun`, the object
      `nih implement` drives - in a fresh repository made from the suite's own
      fixture, with `hidden/<task-id>/` copied in after the run ends and its
      commits are made. `pass_at_1_ratio` needs a task to have committed
      *and* passed; `committed_ratio` is the ceiling on it. Brings
      `first_attempt_gate_ratio` and `revisions_per_task` with it, which §11
      asked of L2 and which belong to a whole task
      ([ADR 0028](adr/0028-a-whole-task-is-graded-by-tests-it-never-saw.md)).
- [ ] Ten tasks per language, against the three **Python** tasks the L3
      suite has. The placeholder named C, Rust and Bash fixtures that were
      never written, and still are not. The shape is proven; the set is one
      language.
- [ ] A fixture per L3 task, for when there is a second language. The suite
      has one fixture and `hidden/<task-id>/`, which is what three Python
      tasks need; a C task needs its own tree. Worth doing when there is a C
      task, not before.
- [ ] The vendored reference sets §11 names - a SWE-bench subset for L3, BFCL
      for L1 - with their licenses next to them and listed in
      `THIRD_PARTY.md`. A download, so not from inside the air gap.
- [ ] Have the planner fill `Task.acceptance`. The field exists and L3 uses
      it; a planner that states what done means per task is strictly better
      than one that points at a spec's criteria by id. It is a prompt change,
      so it needs a golden update and an L1-L3 run in the same commit.
- [ ] Answer the ternary question with it. A natively ternary 27B-class model at
      4-6 GB would change the profile arithmetic entirely - see
      `docs/operations/models.md` - and nothing but an L0-L3 run on this
      project's own suites settles whether it holds up at 64k and on a diff
      that has to apply.
- [ ] Vendored reference sets (SWE-bench subset, BFCL) with their licenses.

## Blocked on the network, by design

Written down rather than worked around (§16.7). The root `NOTES.md` carries
the long version on a working checkout; it is gitignored, because that file
is also where a run's tools write.

- [ ] `certifi` 2026.7.22 -> 2026.10.19 or later. A **critical** advisory:
      the bundled emSign roots have compromised intermediate CA keys. It
      arrives through `httpx` -> `httpcore`. Exposure here is near none - the
      harness's traffic is loopback-only and plain HTTP, so the bundle is
      never consulted - but `require_local` permits `https` to a loopback
      host, so a configuration this project allows would consult it. The fix
      needs an index and a wheelhouse rebuild in the same change: a lockfile
      ahead of the wheelhouse breaks the offline install, which is worse than
      the advisory.
- [ ] `lru` 0.12.5 -> 0.16.3 or later in `tui/Cargo.lock`, a transitive
      dependency of `ratatui`. Low: undefined behaviour in `IterMut` that
      Miri detects, and the TUI does not call it.

## Housekeeping, not blocking

- [ ] Self-hosted Inter and JetBrains Mono under `docs/assets/fonts/`; the docs
      build currently falls back to system fonts.
- [ ] An asciinema recording of the TUI on the landing page.
- [ ] `mike` deployment of versioned documentation.
