<!--
SPDX-FileCopyrightText: 2026 Mateusz Okulanis
SPDX-License-Identifier: GPL-3.0-or-later
-->

# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- The sandbox image builds, and the chain to an air-gapped host was broken in
  five places at once - none of them visible to the quick gate, because the
  gate tests the source tree and nothing tested the image. A `force-include`
  of the prompts duplicated them, so **the wheel would not build at all**, and
  with no wheel there is no wheelhouse, no image and no bundle. The
  instructions in the Containerfile named `uv pip download`, which does not
  exist in uv 0.12, and exported the whole lock including dev groups, some of
  which has no wheel for this interpreter. `bash-language-server` was
  installed as a Debian package and is npm. And the image carried only the
  harness's runtime, so it built without complaint and **could not lint a line
  of Python** - a mistake about what the container is, since the gate runs
  inside it, which is why `ruff`, `mypy` and `pytest` are now installed there
  and `basedpyright` is installed rather than merely downloaded.
- A test builds the wheel and asserts every prompt layer is in it. Nothing
  else in the suite could have caught the packaging bug: tests import from the
  source tree, so a wheel that cannot be built is invisible to them. The first
  version of this test *skipped* - it built through `python -m uv`, which the
  project venv cannot import - which would have been an absent check at
  exactly the moment it mattered; it goes through the `uv` binary that runs the
  gate.
- `replace_in_file` creates a file: an empty `find` writes `replace` as the
  whole content, parent directories included, inside the worktree. Measured
  rather than decided - on the first 14-task L1 run a 4B passed every writing
  task except creating a file, where the harness was pushing it into a
  hand-written creating diff, the format earlier runs had already shown it
  worst at. ADR 0022's objection to a whole-file write does not cover this:
  what it protects is the bound on how much one call may change, and for a
  file that does not exist there is nothing being replaced - the content *is*
  the change and the size cap bounds it. An empty `find` against a file that
  *does* exist is refused, because that would be overwriting (ADR 0025).
  `apply_patch` is now for a change across several places at once, the one
  thing `replace_in_file` cannot do.
- `nih bench --repeat N` runs the suite N times, writes a record per pass as
  it completes, and reports each metric's range plus the tasks that were not
  stable. Two passes of the identical 14 tasks, nothing changed between them,
  differed by three tasks: reading either as a result would have been wrong in
  both directions, including claiming a regression from a change that provably
  fixed the task it targeted.
- L1 grew to 14 tasks and three new expectations, each closing a hole the runs
  exposed. `expect_success` checks that a tool's last call **worked** - being
  called is not having an effect, and a model whose every edit was refused and
  who then ran the tests used to score the sequence; it is what caught the
  creation failure. `forbid_paths` checks that nothing *wrote* where the task
  puts out of bounds, for the policy rule that sends a wrong test to NOTES.md
  rather than editing it quietly; reading is not touching. `expect_content`
  checks the **end state** whatever route reached it, because scoring the
  route would fail a model for choosing `apply_patch` over two edits when
  either is correct - and it is what catches work left half done, which is the
  one failure that repeated across passes.
- A bare `NOTES.md` in `.gitignore` matched at any depth and had been silently
  keeping the L1 fixture's own `NOTES.md` out of the tree. Anchored to the
  root, which is where a run's notes live; run worktrees are under `.nih/`
  anyway.
- `nih bench --layer L1`: the first benchmark runner, and the project's first
  measured numbers for tool calling. Each task runs in its own copy of the
  suite's fixture - a repository of its own, because the tools need one - and
  through the same loop that runs real work, with the same validation,
  detectors, budgets and final artifact. Scoring is a fold over that run's
  events, so the grader is a pure function unit-tested without a model
  (ADR 0024). The suite grew from 5 tasks to 8 of the 20 the methodology asks
  for, and records carry which rule refused each call, with one example of its
  own words - a ratio says how much went wrong, that says what to change.
- Three L1 expectations measured something other than what they claimed, which
  only a run could find: a task pinning `grep` when listing the tree and
  reading the file answers just as well; a refusal task failing a model for a
  harmless call made *after* the refusal, when a refused call never executes;
  and a task asking for `shell`, which is never offered to the role and so
  cannot be called, leaving the rule it expected unable to fire. The first two
  were corrected, the third removed with the reason recorded in the suite.
- A step that demands an artifact is given that artifact's schema when the
  caller does not pass one. The two defaults disagreed - `final_artifact`
  defaults to `final_answer` while `response_format` was None - so a caller
  setting neither got a step whose schema was never sent and whose every prose
  answer was rejected until it errored. Eight benchmark tasks ran to
  `StopReason.ERROR` before the cause was obvious. Derived rather than
  refused, because the schema is the artifact's own and there is one right
  value to fill in.
- An over-long commit subject already got the overshoot and a line that fits;
  a subject with **no type** now gets the same treatment, measured rather than
  guessed at. On the first L1 run, seven of the nine refusals that were not
  the benchmark's own doing were a good subject in plain English with no
  `type:` in front. The refusal now lists the types and offers the model's own
  words with one in place, checked against the length rule too so a suggestion
  cannot fix one rule by breaking another. `valid_calls_ratio` moved 0.796 to
  0.829 and interventions 0.375 to 0.0 across single runs - suggestive at that
  sample size, not significant.
- The sandbox's network is a deployment choice rather than a constant.
  `[sandbox] network` takes `none`, which is the default and what an air-gapped
  deployment requires, or `private` - `slirp4netns:allow_host_loopback=false`,
  so the internet but explicitly **not** this host's loopback, where the
  inference server, the vector store and the management surface listen. The
  rule it replaces conflated two claims: that the harness must *run*
  air-gapped, which stands and is what `require_local` enforces on its own
  traffic, and that a model's tools must never have a network anywhere, which
  was an assumption that bought a connected project nothing. Any other value
  is refused at construction with the two named, because `--network=host` is a
  real mode and must not be reachable by misspelling a line of TOML. Recorded
  in `run.started` as `sandbox_network` and printed by `nih implement` when it
  is not `none`, so silence means the strict default. Nothing else about the
  container changes, and neither does the gate (ADR 0023).
- `replace_in_file`: an edit located by the text it replaces, with the harness
  computing the diff. `find` is copied out of the file and has to occur exactly
  once, `replace` goes in its place, and an empty `replace` deletes it. The
  rule in the tool table relaxes from "unified diff only" to what was always
  the property worth having: every change is a diff by the time anything else
  sees it - and one the harness wrote is well-formed by construction, which the
  hand-written one was not (ADR 0022). `apply_patch` stays for creating a file
  and for a change in several places at once, and when it refuses a diff
  against a file that exists it now names the other tool and how to call it.
  Its refusals are about the passage, and each has an answer: the exact bytes
  when the only difference is whitespace, the closest line when there is none,
  the count when it occurs twice, and "this would change nothing" when `find`
  equals `replace`.
- **The first green commit.** A 4B, unattended: `rag_query` to read a supply
  voltage out of an ingested datasheet whose erratum contradicted the obvious
  answer, `replace_in_file` to write the constant, `format_code` when the
  linter objected, then the gate - `ruff format`, `ruff check`, `mypy --strict`,
  `pytest` - green, and a commit whose body cites the erratum and what ignoring
  it destroys. Verified by re-running the gate on the commit by hand. One task,
  one constant, three runs of which two finished inside the time limit and both
  committed: it says the pipeline closes, not how often or on what size of task.
- An over-long commit subject is told the overshoot and offered one that fits,
  cut at a word boundary and never left ending on "the" or "from". Counting
  characters is the same task as counting hunk lines: a 4B with everything else
  about the step green was told the 72-character limit three times and answered
  99, then 78, then 78. The suggestion is checked against the Conventional
  Commits pattern too, so it cannot fix one rule by breaking another.
- A gate with no commands reports "nothing to run" rather than RED, and names
  the marker file that would configure one. The commit is still refused - a
  gate that verified nothing cannot authorise one - but with a cause that
  exists. Observed on a live run against a project with `.py` files and no
  `pyproject.toml`: told RED with no failing command, the model ran the same
  three checks until the detectors stopped the step. A project that *has* a
  gate but nothing of the requested kind is a third case and says so.
- Retrieval, end to end: a `VectorStore` adapter over Qdrant's REST API, an
  `Embedder` port over the same OpenAI-compatible `/v1/embeddings` the
  inference port already speaks, and `rag_query` answering with chunks that
  cite their lines or their pages. No new dependency: five endpoints with
  `httpx`. `nih ingest` now indexes what it reads, and refuses to report
  success when it could not. Verified against Qdrant 1.19 and bge-m3,
  including an English query retrieving a Polish page that was read by OCR.
  Indexing a document replaces what it indexed before: a chunk's id comes from
  the content, so a revision produces new ids and its predecessor would
  otherwise stay indexed and be cited as current. `VectorStore.forget` exists
  for that, keyed on the path, which is what survives a revision. A document
  inside the project is cited by its project-relative path, so a citation can
  be followed on another machine.
- `rag_query` is served by the harness process, not the sandbox, because the
  container is started `--network=none` and the store is a service on the host
  (ADR 0021). The router now has three destinations and sends it by name;
  tools a local provider answers are declared, so none can claim one the
  sandbox holds. A tool server that offered `rag_query` would be dispatching
  into a container that cannot reach the store, so a contract test asserts it
  does not.
- Retrieval degrades to absence rather than to failure. Both halves are probed
  when a run starts - the store answers, and the embedding model returns
  vectors of the width the collections were built at - and on failure the tool
  is left out of the model's tool list instead of offered and broken.
  `nih implement` and `nih tools` both say so, and `nih tools` reports how many
  chunks each collection holds, because an empty collection and an absent store
  look identical from the outside.
- `apply_patch` names three more mistakes a weak model makes, all found on one
  live run and each with the diff that produced it as a test. `read_file`
  numbers its answer so it can be read; a diff written from what was read
  carries the numbers into the context, where they are not in the file, and
  git then blames a hunk header. The harness added the numbers, so it takes
  them off and says so. A diff of nothing but context lines is valid, applies
  cleanly and changes nothing - reporting success for it is the worst outcome
  available, so it is refused with the sentence that explains it. And added
  lines sent on their own, without headers or a hunk, are named as such
  instead of being called a corrupt patch. A refusal that quotes a `@@` line
  no longer calls it "text with no prefix": a hunk header is legal, and the
  complaint is that it arrived before the previous hunk's counts were used up.
- Logging resolves `sys.stderr` at write time, so a captured or replaced stream
  does not make every later record raise on a closed file. `httpx` is quieted
  to warnings: it logs a plain-text line per request into a stream the harness
  documents as JSON, and there is one per model call, per embedding batch and
  per vector-store request.
- The air-gap check and the rule that a refused request carries the server's
  own explanation live in one place (`adapters/local_http.py`), now that three
  adapters dial a URL from configuration.

- Repository bootstrap: PEP 621 packaging with `uv`, GPL-3.0-or-later licensing,
  REUSE compliance, ruff/mypy/pytest/import-linter configuration.
- Domain core: artifact contracts (`Spec`, `Plan`, `Questions`, `Verdict`,
  `ContextSummary`, `FinalAnswer`), the event catalogue, tool contracts with call
  validation and path confinement, role definitions with per-role allowlists,
  budgets with forced finalisation, the two-tier quality gate with failure
  signatures, tool-loop detectors with the intervention ladder, compaction policy,
  and the event fold that derives run state.
- Tool loop as a pure state machine: it decides the next action and folds what
  comes back into state plus the events to append, so a bounded ReAct loop is
  unit-testable without a model or a container.
- Control plane: commands (`CancelRun`, `ApprovePlan`, `AnswerQuestions`) arrive
  through an append-only inbox beside the event log and become events when the
  run acts on them, so a cancel from the TUI, from `nih cancel` and from an
  agent are one mechanism.
- Management surface: `niharness.management` with the run registry, plan store
  and bounded views; `nih runs`, `nih cancel`, `nih implement --detach`, `--json`
  on the commands that print, and `nih mcp` - an MCP server an agent drives,
  with no path to reading or writing project code.
- The TUI follows a live run (`nih-tui --run <dir>`) and can ask it to stop.
- `nih resume <run-id>` and `run_resume`: a run now records what it concluded
  about each task, so a cancelled run continues without redoing committed work,
  and refuses when it cannot know what it was running.
- The first live model run, against a real `llama-server`, found four things
  that fakes could not. Each is fixed with a test that fails without the fix:
  the artifact's schema was demanded on turns that also offered tools, which is
  a contradiction a model cannot satisfy and a backend refuses outright;
  `apply_patch` rejected diffs over clerical mistakes a model makes constantly
  (missing `a/`/`b/` prefixes, hunk counts counted by eye, a new file announced
  without `--- /dev/null`) and reported git's "corrupt patch at line 8" instead
  of the line and the rule; a backend's refusal reached the operator as "400
  Bad Request" with the server's explanation thrown away; and a reasoning model
  spends the output budget on thinking, with no way to bound it - `[models.*]
  reasoning_effort` now sets the standard field.
- Declared MCP servers: `[[mcp.servers]]` in `harness.toml` names servers the
  harness did not write, the tools to import from each and the roles granted
  them. Tools arrive as `<server>__<tool>`, so nothing a server advertises can
  be mistaken for a built-in one; arguments are checked against the schema the
  server advertised; a declared server that cannot be started, or that does not
  offer an imported tool, stops the run before it begins. `nih tools` prints
  the whole surface per role, starting the declared servers to ask them. A run
  records a `tools.attached` event for each, and none of them can commit.
- Reading documents: `nih ingest` turns a PDF into chunks that cite their
  pages, reading the text layer where there is one and OCRing the pages where
  there is not - `pdftotext`, `pdftoppm` and `tesseract`, no new Python
  dependency. When a page needs OCR is pure policy with two thresholds, and
  OCR has to beat the text layer to replace it. One `Chunk` now carries lines
  (code), pages (documents) or neither (compacted run context), with the
  inapplicable fields absent rather than faked. Verified against a typeset PDF
  and the same document rasterised into an image-only scan.
- `format_code`: the gate's `format` commands in their writing form. A live run
  was refused a correct, typed, documented function for one missing blank line,
  and a model with only `apply_patch` has to guess a formatter's output by
  hand. Every `format` command in the matrix now carries its writing form, a
  test fails if one does not, and the tool is in the implementer's and fixer's
  allowlists only.
- Language servers in the sandbox: `find_symbol`, `find_references` and
  `file_outline`, addressed by name rather than by line and column, so a model
  never has to read a file in order to ask about it. One resident server per
  language, started on demand; the table of which server serves which language
  is domain data beside the gate matrix, overridable under `[lsp]`. When a
  server's index is cold - which is what a real clangd does - the files that
  mention the name are asked directly, so an answer does not depend on a
  background index. `apply_patch` tells the servers what it changed.
- Tool server (`niharness.toolserver`): an MCP server for one worktree, offering
  the harness's own tools, resolving every path against the root, and holding the
  gate - `git_commit` runs the quick tier and refuses with the failing output
  rather than committing on red. Tools answer with an `{ok, text}` envelope,
  because MCP's error channel masks the detail the model needs.
- Application layer with `StepRunner`: runs one step to completion against the
  ports, performing what the loop decides and appending the events it returns.
- `Window`: measures the model's window through the `Tokenizer` port, applies the
  compaction policy before every model call, emits `context.compacted`, and keeps
  the message list in the order the protocol requires.
- `ModelSummarizer`: produces a `ContextSummary` with the utility model when the
  deterministic compaction passes are not enough, and drops any file it claims
  to have seen that the summarised text never mentioned.
- Ports: `ModelBackend`, `Tokenizer`, `ToolProvider`, `VectorStore`, `Vcs`,
  `Sandbox`, `EventSink`.
- Adapters: JSONL event sink, OpenAI-compatible inference client, git worktree
  adapter, the rootless Podman sandbox (one hardened container per command, with
  the argument vector asserted by a contract test), and the MCP tool client
  (only known and allowed tools are forwarded to the model).
- CLI (`nih`) with `init`, `config`, `gate`, `plan check`, `plan import`,
  `status`, `docs generate`, and `implement` - which runs an approved plan in a
  dedicated worktree with the tool server in the sandbox, taking each task
  through implement, the commit gate, review, and bounded fixes.
- Prompt layers for every role, plus the Definition of Done checklist.
- Documentation site (MkDocs Material) with concept pages, a generated event
  catalogue and the first architecture decision records.
- Rust TUI crate skeleton (ratatui) that renders the run event stream.
