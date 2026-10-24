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

- **The first L2 numbers, and what they say.** Three passes of the eight cases
  on a 4B: precision **1.0 - 1.0**, recall **0.25 - 0.5**. It never refused a
  correct change and missed half to three-quarters of the defects - not a good
  reviewer but an agreeable one, and precision alone would have read as
  excellent. What it caught and what it missed divide cleanly: the defect it
  found every pass is visible in the diff, and the three it missed every pass
  are defined by something *absent* - an error swallowed and replaced with a
  plausible value, a test that asserts a type instead of a behaviour, a
  document left stale. When it did catch one it named the thing every time, so
  the gap is detection rather than articulation, which says the prompt is not
  where to look.
### Changed

- **Measured: `reasoned_ratio` 1.0 - 1.0 to 0.5 - 0.5.** Three passes with
  the strict key, and the metric distinguishes something for the first time.
  One case says what the old key was hiding: `documentation-left-behind` was
  refused in all three passes, correctly, and the notes never mention a
  document or `docs/limits.md` at all - it scored as reasoned under the old
  key because `0.5` was a token and `0.5` is on the line the diff removes. A
  NAK that was right by accident counted as a NAK that was right. And the key
  is not measuring vocabulary: the other case that stops passing fails on
  *where*, anchoring its note to the test file rather than the source.
- **L2's answer key no longer scores a quoted diff.** `reasoned_ratio` read
  1.0 across six passes while distinguishing nothing, because `LOWER_BOUND`,
  `except` and `ValueError` are all on lines their cases change. One rule now
  governs `mentions_any` - no token may appear in the case's own diff, either
  side - and a test checks it against the case files, because the next case
  will be added by somebody who has not read the paragraph. The replacement
  tokens are a generous set of ways to name each defect, since demanding a
  phrasing would measure vocabulary instead of judgement. `reasoned_ratio`
  before and after this is not comparable; it waited for the evidence check's
  before-and-after to conclude.

### Added

- **Both records say which tools were called, not only how many times.** A
  reviewer refused a correct change because "the tests cannot be run", in a
  pass where other cases ran them green - and `calls: 2` cannot tell a
  refusal that tried from one that never attempted it. Each task and case now
  carries a `tools` tally, summed over the suite and printed next to the
  metrics, folded from `tool.called` exactly as the sequence metrics are. It
  is the fact the inverse-claim check on the roadmap needs, and the second
  time in two days that a question about a model could not be answered from a
  record taken while the question was being asked.
- **The tally's first run answered the question the ratios could not.** Three
  L2 passes at `c63c5b6`: every intervention in those twenty-four reviews was
  `exact_repeat`, and `unbacked_claim` does not appear once. The evidence
  check has never fired in a recorded run, so the three passes taken to
  measure it measured nothing about it - which is what a cause recorded next
  to a count is for. What the check does is still right; the suite does not
  provoke it, and making it measurable needs a case about the model's
  incentives rather than about a defect.
- **A reviewer confabulated a broken environment to justify a refusal.** The
  same correct change was refused in two of six passes because "tests cannot
  be run due to missing Python environment" and "cannot verify build and
  tests without Python language server". The record disproves it: four or
  five cases in those same passes were ACKed, an ACK requires
  `tests_passed`, and `evidence_ratio` was 1.0 - which says a `run_tests`
  call returned green in each of those steps. The inverse claim is as
  checkable as the one the harness already checks, and is on the roadmap with
  the measurement it would need.
- **Every metric that varies is drawn as well as tabulated.** A bar from a
  column's low to its high with a dot per pass, so the thing a table makes
  you work for - two spreads that overlap have not shown a difference - is
  visible at a glance. Hand-written SVG: no new dependency, and no colour of
  its own, so the one drawing follows the page into dark mode. A ratio is
  drawn against 0 to 1; anything else against its own labelled low and high,
  because three passes spanning 99.22 to 100.02 are a tight measurement that
  a zero-based axis draws as a half-pixel smudge at the ceiling. Nothing is
  drawn where nothing varies, since a full-height bar for a single number
  invents a comparison.
- **`nih bench report`, and the results page is generated from the records.**
  A column is one harness commit, a cell is the range across that column's
  passes, and a second table says how many of a column's passes each case or
  task passed. `nih docs generate` renders `docs/benchmarks/results.md` with
  the same function, so a page and a terminal cannot disagree about a
  measurement, and the page no longer says there are no results while the
  records directory holds them. Every before-and-after
  comparison this project has made was assembled by hand from terminal
  output, which is how one set of passes came to be described from memory
  after its record had been deleted.
- `nih bench --layer L2` keeps working next to `nih bench report`: the group
  routes anything that is not a subcommand to `run`, the way `nih plan
  SPEC.md` already routes to `plan spec`.
- **Both benchmark records say which detector fired.** `loop_incidents` says
  how much the ladder interfered and the highest level says how far it
  climbed; neither says what for, so a change to one detector could not be
  measured at all - the three L2 passes above are consistent with the evidence
  check firing and with its never having fired once. Each case and task now
  carries a `pathologies` tally, summed over the suite and printed next to the
  metrics. A tally with the new detector missing from it is the measurement
  saying it measured nothing, which is the reading that was unavailable.
- **The evidence check was measured, and the measurement was designed
  wrong.** Three passes at `73a5c99` against three at `50a13ef`: precision
  **1.0 - 1.0 to 0.75 - 1.0**, recall **0.25 - 0.5 to 0.25 - 0.75**,
  `passed_ratio` **0.5 - 0.625 to 0.625 - 0.75**, and `evidence_ratio` 1.0 in
  both. The metric the check acts on was already at its ceiling before the
  check existed, and an unbacked claim happened once in thirty-two reviews -
  twenty-four clean reviews are what that rate produces by itself about half
  the time. Every other range overlaps. The set of cases failing every pass
  shrank from three to one, and the two that left it now pass one and two
  times out of three, which is unstable rather than fixed. Recorded as what it
  is: no evidence either way, from passes that could not have provided it.
- The after-passes did record the suite's **first false positive** in
  fifty-six reviews - a NAK on a correct change, reasoned as "cannot verify
  build and tests without Python language server". That is the risk this check
  carries, stated when the check was added: pressure to back a claim can
  become a refusal to decide. One occurrence, and the reason is wrong on its
  own terms, so it is a thing to watch rather than a result.
- **A verdict's claims are checked against its own step.** `Verdict` asserts
  that the build and the tests passed, §4 has always said the reviewer runs
  both itself, and nothing checked whether it did. A verdict whose claims no
  call in its own step backs is now sent back once, with the claim named and
  tools still available.
- Found by measuring, not by reading the code: on the first L2 run a 4B ACKed
  a change after two tool calls, neither of them `run_tests`, while asserting
  that the tests passed. One case in eight.
- Asked once, and never inside forced finalisation. A model that answers
  unbacked a second time has its verdict accepted and the unearned claim
  recorded - a step that cannot end is worse than a claim the log already
  catches, and `evidence_ratio` is what catches it. The harness does not touch
  the decision: a review is the model's judgement, and what is refused is
  recording evidence nobody gathered.
- `CallRecord` carries `failed`, so "the tests passed" can be told from "a
  `run_tests` call came back red", and `loop.intervention` carries `detail`,
  so the tally of pathologies says what the model actually did.
- **The spec gate's reviewer half.** One pass, no tools, over `SPEC.md` before
  planning starts, looking for what the schema cannot see: a criterion that
  would hold whether or not the work was done, a goal nothing would detect the
  absence of, a contradiction between the goal and the constraints.
- It answers with a `SpecVerdict`, not a `Verdict`. An ACK on a `Verdict` has
  to stand on a green build and green tests, and a spec has neither - an
  artifact whose required fields are meaningless for the thing it describes
  gets filled in with whatever validates. A NAK has to name what is missing,
  enforced by the model, because a refusal the author cannot act on is worse
  than no refusal.
- Its prompt stack is the policy plus `15-gate-spec.md` and not the reviewer
  role's: that layer is about one committed change, and would be instructions
  for the wrong job. One step's stack is therefore not a role's, which is now
  a snapshot of its own in the golden set.
- **Entry B works: `nih plan SPEC.md` plans a spec.** The spec is validated
  offline, a worktree is made, the planner reads the code with read-only tools,
  and `PLAN.md` is written and stored by digest. `nih plan approve PLAN.md`
  records the decision, which is what `nih implement` then runs. Two of three
  entry points now work end to end.
- **`nih bench --layer L0`: throughput, by the runner rather than by hand.**
  Three timed requests per context size, using the server's prefix cache as
  the instrument rather than fighting it: the repeat of a single-token request
  costs the per-request overhead and nothing else, so subtracting it from the
  cold request leaves the prompt and from the long one leaves the generation.
  No backend timings are read - the port is HTTP and nothing more. It agrees
  with the hand-taken `llama-bench` figures to 1.3% on prompt processing, with
  a 0.2% spread across three passes.
- Getting there took two wrong versions, and the second is the instructive
  one. With an identical prompt in both of two requests, the cache answered
  the second from the first and the **long request came back 3.6 seconds
  faster than the short one** - obviously broken. Giving each request its own
  marker fixed that and introduced something worse: the markers repeated
  between *runs*, so three passes reported **68,769 tok/s** on a laptop GPU
  with a spread of 1,386 to 17,070. Numbers rather than errors, which is the
  dangerous kind of wrong. The method now uses a nonce per invocation and
  checks itself: if the first request was not cold, it reports no prompt rate
  instead of an enormous one.
- **`nih bench --layer L2`: the review layer.** Eight golden cases, four ACK
  and four NAK, each one the fixture plus a directory of replacement files
  committed as one change - so the reviewer reads a diff git produced, a case
  cannot stop applying, and a case can be read rather than reconstructed from
  hunks. Every case is green on format, lint and tests, which follows from
  where review sits: the gate runs first, so a defect the tests catch never
  reaches a reviewer. Every NAK is a defect no test can catch.
- Precision and recall take NAK as the positive class, stated rather than
  inferred, and a case that produced no verdict at all is counted as neither -
  folding an absent reviewer into recall makes it look cautious.
- A NAK is scored on *where* and *why*: a note anchored to one of the case's
  `defect_files`, and one of several `mentions_any` tokens in the reviewer's
  own words. The `L2Case` model refuses a NAK case that declares neither, so
  the suite cannot grow a case whose answer key is "said NAK" - which a
  reviewer that refuses everything would pass with full marks.
- `evidence_ratio`: a verdict claiming the tests passed has to point at a green
  `run_tests` call in its own step. The reviewer exists because an
  implementer's claim is not evidence, and this is the same standard applied to
  the reviewer; a case fails without it however right the decision was
  (ADR 0027, `docs/adr/0027-a-review-case-is-a-tree-and-its-verdict-must-be-earned.md`).
- The suite's green invariant is a test, not a habit: each case is copied over
  the fixture and put through the fixture's own gate. A planted syntax error
  fails it, which is the only way to know a check like that is doing anything.
- The previous `benchmarks/suites/l2/golden.toml` declared three cases pointing
  at `.patch` files that were never written - the same declared-but-absent hole
  `rag_query` and `add_dependency` had. It is now a real suite.
- `nih run SPEC.md`: both halves of the pipeline, with no approval stop. It
  chains the two fresh runs and approves the plan for you - and refuses to
  implement a plan whose `open_questions` are not empty. The only stop this
  command removes is the human reading of the plan, so a planner that says it
  had to guess is exactly what that stop was for; the plan is left approved and
  the questions printed, and `--ignore-open-questions` says you have read them.
- `PlanDraft`: what the planner actually writes. A `Plan` carries the spec's
  digest, and asking a model to reproduce a digest of a document it was shown
  as prose is asking for transcription rather than judgement - the one field in
  the artifact with exactly one right value that the harness already knows. The
  planner writes the tasks; `PlanDraft.for_spec()` binds them to the spec.
  Every rule about the tasks stays on `Plan` and fires at the moment of
  binding, so a draft whose dependencies point forwards is reported as a
  planning error with the offending id named, not as a schema failure
  (ADR 0026, `docs/adr/0026-the-planner-writes-the-work-the-harness-binds-the-spec.md`).
- The planner's window gets the spec once. `nih plan` renders it for the model
  rather than reusing the `SPEC.md` renderer, which repeats the whole artifact
  as a JSON block for the gate - and the digest appears in neither.
- `add_dependency` exists. It was in the domain and in two roles' allowlists
  and the tool server had **never registered it**, so a model that called it
  got "the tool server failed to run add_dependency" - the same
  allowlisted-but-absent hole `rag_query` had, left behind for the other name.
  It now does what §4 documented all along: refuses to install anything,
  because the run is air-gapped, and records the request in `NOTES.md` for
  somebody to decide about later. Asking twice does not write it twice, and
  asking for something already present says so instead of filing a note.
- Its answer is deliberately **not** a failure. What the model asked for
  cannot happen, so a red result invites it to ask again, and this week's
  measurements are largely a record of models repeating refused calls. "Nothing
  installed, recorded, carry on with what is here" ends the dead end instead
  of looping on it.
- This is also the first code in the project that writes `NOTES.md`. The
  prompts point at it, the refusal messages point at it, the event catalogue
  has `notes.appended` - and nothing had ever created the file.
- **`add_dependency` measured, and what the suite could not see.** Three clean
  passes at `e62dc3e` against three at `6e43ea8`, same model, same host:
  `write-down-what-cannot-be-done` went from **0/3 to 3/3**. That is the task
  `add_dependency` was written for and the clearest per-task result in the
  suite so far. The aggregate did not move - `passed_ratio` 0.65-0.85 before,
  0.65-0.90 after, overlapping ranges - because one task of twenty is 0.05 of
  that ratio and the pass-to-pass spread is 0.25. Worth stating as a property
  of the instrument rather than of the model: at twenty tasks and three
  passes, this suite cannot see a one-task change in its headline number, and
  the per-task table is where such a change is visible at all.
- Two tasks still fail every pass. `two-places-must-agree`: the model changes
  one of the two files that have to agree and commits. `recover-from-an-ambiguous-passage`
  went 1/3 to 0/3, which at this sample size is noise and not a regression.
- **Correction, and the first provenanced record of the full suite.** Three
  passes of the 20 tasks from one clean commit put `passed_ratio` at
  **0.65 - 0.85** - thirteen to seventeen of twenty. A single pass taken right
  after the refusal-quoting change read 0.85, and quoting that as the result
  took the top of a four-task range for its centre. The direction holds: the
  one "before" pass was 0.55, below the whole of the after-range. The
  magnitude does not, and the single pass should not have been reported
  without the repeats - which is the same mistake `--repeat` was built to
  prevent, made while the feature sat there unused.
- Six of the twenty tasks pass once or twice in three, and two fail every
  time: `two-places-must-agree`, where the model changes one of two files and
  commits, and `write-down-what-cannot-be-done`, where the policy says to
  record an unavailable dependency in `NOTES.md` and it does not. The second
  is the cleanest test of obedience to a stated rule in the suite rather than
  of any skill.
- A degenerate response never enters the window. Refusing it in the loop was
  not enough: the executor recorded the assistant turn *before* the loop
  judged it, so the junk was already in the window and the **next**
  `prepare()` asked the backend to render a tool call whose JSON had been
  truncated mid-string - which answered 500 and ended a run rather than a
  step. It happened twice, the second time after the loop had been taught to
  refuse it, which is what made the ordering visible. The executor now asks
  the same pure function the loop uses, before adding the turn, so the two
  cannot disagree about what counts as degenerate.
- A degenerate response is refused rather than logged, and the check now reads
  a tool call's arguments as well as the prose. The rule said "abort
  generation"; the code emitted a `loop.detected` event and then dispatched the
  call anyway. Observed on a live run: a 4B emitted
  `last_retry_time_ms_ns_ns_ns...` growing inside a `replace_in_file` argument
  until the tool call's JSON was truncated mid-string, the prose was empty so
  the detector saw nothing, and the backend returned 500 when the harness next
  asked it to render that history - which ended a benchmark run rather than a
  step. It is now recorded as an invalid call, nothing is dispatched, and the
  model is told its answer repeated itself, so a model that keeps doing it
  ends the step through the ladder instead of through an HTTP 500.
- A repeated call that had been **refused** gets the refusal's own words back,
  not a hash of them. The level-one intervention interpolated
  `Digest of the previous result: 3f9a...`, which is nothing a model can act
  on, and it did so at exactly the moment the reason most needed repeating.
  Measured on the 20-task L1 suite: every one of nine failing tasks ended with
  the same two interventions, and the pattern underneath was always a tool
  refusing with a message saying what to do, the model re-sending the
  identical call, and the ladder stopping the step. A refused result now keeps
  its own text in the loop's memo rather than a digest, bounded, and the
  repeat quotes it.
- Compaction writes what it drops into the run's own collection, which is what
  makes it lossless from the model's side. The event log kept everything
  regardless - compaction is a view policy and never touches it - but the
  *model* cannot read the event log, and `docs/concepts/context.md` had been
  describing this recovery as a fact since before it existed. A dropped item is
  chunked with a content-derived id, so a step rolled back and retried writes
  the same material once, and an item too large to embed well is split on a
  paragraph break, then a line break, then at the limit - three boundaries,
  each worse than the last, the same ordering the document reader uses on an
  oversized page. `ChunkPolicy` moved to `domain/rag.py` beside `Chunk`, where
  it belongs and where the run-context chunker can reach it without a circular
  import.
- A vector store that is down costs the model its recall and not the step. The
  window is already compacted by the time the archive is written, so failing
  there would trade a recoverable loss for an unrecoverable one; it is logged
  and `context.compacted` reports `indexed_chunks: 0`, a field that had existed
  and never been populated.
- L1 reaches the 20 tasks the methodology asks for, and `forbid_content` with
  them: asserting what a file contains cannot say that something was removed,
  which is the whole point of a deletion task. The six new ones are scored on
  end state and effect rather than route, on the evidence of three passes where
  the route metrics moved by up to three tasks in fourteen with nothing
  changed. The fixture's baseline is now stated as an invariant - format clean,
  lint clean, exactly one deliberate test failure - because a task that adds a
  second red thing makes every gate-running task fix somebody else's problem.
- A record reads the harness commit once, at the start of the run, as the dirty
  flag already did. It was read as each record was written, so a run that
  straddled a commit labelled its later passes with code they were not
  running - which happened, and produced a record naming a commit whose
  changes it had never loaded.
- **Correction.** The commit that quoted `recover-from-an-ambiguous-passage`
  going "0/3 to 1/3 - moved, not solved" was reading a measurement taken on a
  working tree. Three passes from a clean tree fail it 3/3. The one success
  was variance, the disambiguation fix did not measurably move that task, and
  the claim should not have been made from a dirty record - which is the
  reason records now carry `harness_dirty` and the reason that flag says a
  dirty record is a working note.
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

### Changed

- A task's fixture is copied without tool caches. Checking the suite's
  baseline invariant means running `ruff` and `pytest` in the fixture
  directory, which leaves a cache behind - and the fixture was then committed
  with it, so the tree hash the state-cycle detector compares depended on
  which ruff somebody last ran by hand, and two machines prepared measurably
  different fixtures from identical checked-in files.

- `spec.accepted` carries `reviewed`. The spec gate has two halves - a schema
  check and a reviewer pass - and only the first one exists, so the event says
  which ran instead of implying a review nobody performed.
- The control inbox is drained in one place. `ControlDrain` turns requests into
  events for every kind of run, so a cancel from the TUI, from `nih cancel` and
  from an agent arrive the same way whether the run is planning or
  implementing.
- The wiring both model-running commands need - a backend client, how to start
  the tool server, the sandbox readiness check, and the two lines about network
  and retrieval - moved to `cli/wiring.py`, so the two stages cannot disagree
  about what a run looks like.
- The planner's prompt asks for a `PlanDraft` and sends open questions to
  `open_questions` rather than to a `Questions` artifact: a step enforces
  exactly one final artifact, and nothing produces `questions.asked` yet.
