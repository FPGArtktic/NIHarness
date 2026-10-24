<!--
SPDX-FileCopyrightText: 2026 Mateusz Okulanis
SPDX-License-Identifier: GPL-3.0-or-later
-->

# Benchmark methodology

Four layers, because "the model is good" is not a measurement. Every record is
JSONL tagged with the harness commit, prompt version, model, quantization,
profile and date, and committed under `benchmarks/results/`.

| Layer | What it measures | Metrics |
|---|---|---|
| L0 inference | backend × profile × quantization | prompt-processing and generation tok/s at 8k / 32k / 64k, RAM, VRAM, load time |
| L1 tool calling | 20 tasks, 3-5 calls each | syntactically valid calls, correct sequences, correct arguments, terminated loops, loop incidents per task |
| L2 steps | gate and review on a golden set | first-attempt gate pass rate, revisions per task, reviewer precision and recall against human verdicts, diff size, highest intervention level reached |
| L3 end-to-end | 10 tasks per language, hidden tests | pass@1, wall time, tokens, tool calls, harness overhead outside inference |

## Running L0

```bash
uv run nih bench --layer L0
uv run nih bench --layer L0 --repeat 3
```

How fast this host is, through the port the harness actually uses. Every step
budget is fitted to these numbers, which is why they belong in the same record
format as the layers that grade something - and why they should not be taken
by hand.

### Two requests, and the arithmetic between them

Each probe sends the same prompt twice: once generating a single token, once
generating `generate_tokens`. The difference between them is generation alone:

- `generation_tps = (generated - 1) / (full_seconds - first_seconds)`
- `prompt_tps = prompt_tokens / (first_seconds - 1 / generation_tps)`

No backend timings are read. The port is OpenAI-compatible HTTP and nothing
more, so a measurement that depended on `llama.cpp` reporting its own numbers
would be a backend-specific code path with extra steps. The prompt is counted
by the backend's tokenizer, never estimated from characters - a throughput
divided by a wrong token count is a wrong throughput.

### The prefix cache will ruin this if you let it

The first version of this ran the two requests with an identical prompt. The
long one came back **3.6 seconds faster** than the short one, because
llama-server caches the prompt and the second request never reprocessed it. A
negative duration is not a generation rate.

So each request carries a different marker in its **first** line. First,
because a prefix cache matches a prefix: appending a nonce changes nothing.

### What it is not

Not `llama-bench`. That tool controls batch sizes, warms up and repeats, and
it measures a different journey: no HTTP, no server scheduling, no tokenizer
round trip. Its numbers are better controlled; these are the ones the harness
lives with.

The two agree closely enough to believe both. Ternary Bonsai 2 4B in `PQ2_0`
on an RTX 3050 Laptop: `llama-bench` said 1539 prompt / 105.6 generation,
this said **1444.8 / 107.17** at 2k of context - within about 7% on the prompt
rate and inside the noise on generation.

### What it does not measure

Load time, which §11 asks for. Measuring it means owning the server's
lifecycle, and the harness does not start the backend. A probe that needs more
tokens than the served context window can hold - prompt *plus* its own
generation - is skipped and named in the record, rather than sent and recorded
as a zero that would read as "infinitely slow at 64k".

## Running L1

```bash
uv run nih bench --layer L1                        # in the sandbox
uv run nih bench --layer L1 --unsafe-local-tools   # on this host
```

Each task gets a fresh copy of `benchmarks/suites/l1/fixture/`, made into a
git repository of its own. The tasks act on that and never on this repository:
a target that changes with every commit makes two records incomparable, which
is the one thing a benchmark may not do. A repository rather than a bare
directory because the tools expect one - the gate commits, `apply_patch`
reverses a diff to tell "already applied" from "does not apply", and the
state-cycle detector hashes the tree.

The task runs through the **same loop that runs real work**: the same
validation, the same allowlist, the same detectors, the same budgets, and the
same `FinalAnswer` artifact demanded at the end. Two things follow. A number
is about the model rather than about a rig that resembles the harness, and
"terminated the loop" means the model produced its artifact - not merely that
something stopped.

### One pass is not a measurement

```bash
uv run nih bench --layer L1 --repeat 3
```

Two passes of the identical 14 tasks, with nothing changed between them,
differed by three tasks. Reading either as a result would have been wrong in
both directions. So a pass is a record, several passes are several records in
one file, and the human output reports each metric's range and names the tasks
that were **not stable** - a task that passes once and fails once has said
something different from one that passes every time, and a single number
cannot carry the difference.

### Prefer an expectation about effect over one about route

Three passes of the identical suite, nothing changed between them. The
20-task set at `6e43ea8`, on a 4B:

| Metric | Range across 3 passes |
|---|---|
| `passed_ratio` | 0.65 - 0.85 |
| `correct_sequence_ratio` | 0.95 - 1.0 |
| `correct_arguments_ratio` | **1.0 - 1.0** |
| `effective_ratio` | 0.95 - 1.0 |
| `complete_ratio` | 0.8 - 0.9 |
| `terminated_ratio` | 0.95 - 1.0 |
| `loop_incidents_per_task` | 0.55 - 0.8 |

`passed_ratio` spans four tasks in twenty with the code unchanged, which is
the number to keep in mind before reading any single pass as a result. Six of
the twenty passed once or twice out of three; two failed every time.

An earlier three passes of the 14-task set showed the same shape and
sharpened one point: the metrics about **effect** and **end state** did not
move at all while the route metrics varied by up to three tasks, and both
failures that repeated across every pass were caught *only* by those two.

So a new task should prefer `expect_content` and `expect_success` over
`expect_sequence`, and the reason is not only stability: an expectation about
route also fails a correct model that took another path, which is the mistake
two of this suite's first tasks made.

### Scoring is a fold over the events

Nothing grades a transcript. Each task is scored from the payloads its run
appended, in `niharness.domain.bench`:

| From | Metric |
|---|---|
| `tool.called` names, in order | `correct_sequence_ratio` |
| `tool.called` arguments | `correct_arguments_ratio` |
| `tool.called` against `tool.rejected` | `valid_calls_ratio` |
| `tool.rejected` violations | a refusal task's expectation |
| `loop.intervention` | `loop_incidents_per_task` |
| `step.finished.stop_reason` | `terminated_ratio` |

That makes the grader a pure function, unit-tested on recorded sequences with
no model involved - so a number it produces says something about the model and
not about the grader.

Extra calls around the expected ones pass: reading a file before changing it
is not a mistake. `strict_sequence = true` turns that off.

A task whose answer is a **refusal** expects the named `Violation` to fire.
Harmless calls after it do not fail the task - a refused call never executes,
so the forbidden thing cannot have happened by any route.

Two expectations say what a sequence cannot:

| In the suite | What it checks |
|---|---|
| `expect_success = ["replace_in_file"]` | the tool's **last** call worked. Being called is not having an effect, and a model whose every edit was refused and who then ran the tests would otherwise score the sequence. The last call, because recovering from a refusal is a skill |
| `forbid_paths = ["tests/"]` | nothing **wrote** there. Reading is not touching: a model told a test encodes the right behaviour should read that test, and the policy forbids editing it quietly |
| `expect_content = { "src/a.py" = ["LIMIT = 5"] }` | the **end state**, whatever route reached it. A change two tools could make equally well should not be scored on which was used - and this is what catches work left half done |

Neither is host-dependent, which is why the symbol tools have no tasks here:
`find_symbol` and `file_outline` need a language server in the image, and a
metric that moves with what happens to be installed is not a metric.

## Running L2

```bash
uv run nih bench --layer L2                        # in the sandbox
uv run nih bench --layer L2 --unsafe-local-tools   # on this host
uv run nih bench --layer L2 --repeat 3             # and the spread
```

L2 measures **review**: given a change and the task it claims to do, does the
reviewer reach the verdict a human reached, in the right place, for the right
reason, on evidence it gathered itself.

### A case is a tree, not a patch

A case is `benchmarks/suites/l2/fixture/` plus the files in
`benchmarks/suites/l2/cases/<id>/`, committed as one change. The diff the
reviewer reads is produced by git from those two trees.

Not a stored `.patch`, for two reasons. A patch stops applying the moment the
fixture moves, and a golden set whose cases silently fail to apply is worse
than no golden set. And a case written as replacement files is a case somebody
can *read*: the question "what is wrong with this change" is answered by
looking at the file, not by reconstructing it from hunks.

### Every case is green

Format, lint and tests pass on the baseline fixture **and after every case**.
That is the suite's invariant, and it follows from where review sits in the
pipeline: the gate runs first, so a defect the tests catch never reaches a
reviewer. Every NAK in this suite is a defect no test catches - a swallowed
error, a test that cannot fail, a document left behind, a boundary that no
longer matches its own docstring. Those are the defects a reviewer is for.

The L1 fixture, by contrast, carries one deliberate test failure. Reusing it
here would make every honest verdict a NAK, because `Verdict` refuses an ACK
over red evidence.

### NAK is the positive class

The reviewer's job is finding the defect, so a NAK is a detection:

- **precision** - of the changes it refused, how many deserved it
- **recall** - of the defects that were there, how many it found

Read the other way round the numbers mean the opposite, so the suite states
the choice rather than leaving it to be inferred.

A case that produced **no verdict** - a step that ran out of budget - is
counted as neither. Folding it into recall would make a model that was absent
look cautious.

### A NAK has to be for the right reason

| In the suite | What it checks |
|---|---|
| `verdict = "NAK"` | the decision matched the human's |
| `defect_files` | a note is anchored to one of them. Several are allowed: a stale document and the code that outdated it are both right answers |
| `mentions_any` | at least one token appears in the reviewer's own words. *Any*, not all, because a review is free text and demanding a phrasing measures vocabulary |

A NAK case without both is rejected when the suite loads. Without that rule
the suite would score "said NAK", which a reviewer that refuses everything
passes with full marks.

### The reviewer's own claim is not evidence

The reviewer exists because an implementer's claim is not evidence. The same
standard applies to it: a verdict that says the tests passed has to point at a
`run_tests` call *in its own step* that returned green. `evidence_ratio` is
how often that was true, and a case fails without it however right the
decision was.

### What L2 does not measure yet

§11 asks L2 for the implementer's side too - first-attempt gate pass rate and
revisions per task over a golden set of tasks. That needs implement, gate,
review and fix run end to end, which is closer to L3's shape than to this
one. The review half is what exists; see [the roadmap](../roadmap.md).

## Rules

- A record from a working tree carries `harness_dirty: true` and is a working
  note, not a result. Two measurements taken either side of an uncommitted
  change otherwise report the same `harness_commit`, which is the one thing a
  record must not do.
- A prompt or model change without an L1-L3 run in the same change is
  incomplete. The claim "this prompt is better" has a number or it does not
  exist.
- Reference sets are vendored for offline use - a SWE-bench subset for L3, BFCL
  for L1 - with their licenses next to them and listed in `THIRD_PARTY.md`.
- Harness overhead is reported separately from inference time. If the harness is
  slow, that is the harness's problem, and hiding it inside model latency would
  be convenient and dishonest.
- `nih bench report` renders Markdown from the records; the TUI Bench view reads
  the same records, so a chart and a table can never disagree.

## Why loop incidents are a first-class metric

A model that finishes with a correct patch after sixty redundant calls is not
equivalent to one that finishes in eight. The intervention ladder makes that
visible as an event, so it becomes a number in L1 and L2 and therefore a
model-selection criterion.
