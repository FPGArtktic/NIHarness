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

### Three requests, and the arithmetic between them

Each probe sends the same prompt three times: once generating a single token
on a prompt the server has never seen, once generating a single token on the
same prompt, and once generating `generate_tokens` on it again. The prompt
carries a nonce in its first line, which is what makes the first request cold.

The server's prefix cache is the **instrument** rather than the enemy. The
second request repeats the first exactly, so it pays the per-request overhead
and one token of generation and nothing else:

- `generation_tps = (generated - 1) / (long_seconds - warm_seconds)`
- `prompt_tps = prompt_tokens / (cold_seconds - warm_seconds)`

No backend timings are read. The port is OpenAI-compatible HTTP and nothing
more, so a measurement that depended on `llama.cpp` reporting its own numbers
would be a backend-specific code path with extra steps. The prompt is counted
by the backend's tokenizer, never estimated from characters - a throughput
divided by a wrong token count is a wrong throughput.

### The prefix cache will ruin this if you let it

This took two attempts, and the first one produced numbers rather than
errors - which is the dangerous kind of wrong.

The first version sent **two** requests with an identical prompt and
subtracted. The long one came back 3.6 seconds *faster* than the short one,
because the second request never reprocessed the prompt. A negative duration
is at least obviously broken.

The fix - a different marker in each request's first line - made the two
requests differ from each other but not from the previous *run*. Three passes
then reported a prompt rate of **68,769 tok/s** on a laptop GPU, with a spread
of 1,386 to 17,070 between passes. Every prompt after the first run was served
from the cache.

So: a nonce unique to each probe invocation, and a method that checks itself.
If the first request was not cold, `cold_seconds` and `warm_seconds` are the
same, and the probe reports no prompt rate instead of an enormous one. The
floor is `warm_seconds` itself - what a request costs when it does almost
nothing is this method's own resolution, and a prompt that took less than that
to process cannot be timed this way.

### What it is not

Not `llama-bench`. That tool controls batch sizes, warms up and repeats, and
it measures a different journey: no HTTP, no server scheduling, no tokenizer
round trip. Its numbers are better controlled; these are the ones the harness
lives with.

The two agree closely enough to believe both. Ternary Bonsai 2 4B in `PQ2_0`
on an RTX 3050 Laptop, three passes:

| | `llama-bench` | `nih bench --layer L0` |
|---|---|---|
| prompt processing | 1539 | **1518 - 1522** at 8k |
| generation | 105.6 | **98.8 - 100.0** at 2k |

Within 1.3% on prompt processing and 5% on generation, with a pass-to-pass
spread of 0.2% and 1.2%. A method that disagreed with a known-good tool by an
order of magnitude - which the second version did - is a method that measured
its own cache.

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
| `loop.intervention.pathology` | the `pathologies` tally |
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

`mentions_any` is currently **lenient**, and the first three passes show it:
`reasoned_ratio` was 1.0 throughout, which means it distinguished nothing.
Some of the tokens appear in the diff itself - `LOWER_BOUND` is on the line
that changed - so a note anchored there is scored as reasoned almost
regardless of what it says. Tightening it to the tokens that actually
discriminate (`inclusive`, `lower bound`, `limits.md`) would make the metric
informative, and is deliberately not done between a before and an after
measurement: changing the answer key makes the two incomparable. It is on
[the roadmap](../roadmap.md).

### The reviewer's own claim is not evidence

The reviewer exists because an implementer's claim is not evidence. The same
standard applies to it: a verdict that says the tests passed has to point at a
`run_tests` call *in its own step* that returned green. `evidence_ratio` is
how often that was true, and a case fails without it however right the
decision was.

### What the first L2 runs said

Ternary Bonsai 2 4B in `PQ2_0`, three passes over the eight cases, at
`50a13ef`:

| Metric | Range across 3 passes |
|---|---|
| `reviewer_precision` | **1.0 - 1.0** |
| `reviewer_recall` | 0.25 - 0.5 |
| `decision_ratio` | 0.5 - 0.75 |
| `located_ratio` | 0.5 - 1.0 |
| `reasoned_ratio` | 1.0 - 1.0 |
| `evidence_ratio` | 1.0 - 1.0 |
| `passed_ratio` | 0.5 - 0.625 |

Three findings, and the first is the one worth the suite:

**It never refused a correct change, and missed half to three-quarters of the
defects.** Precision 1.0 with recall 0.25 is not a good reviewer; it is an
agreeable one. Precision alone would have read as excellent, which is why the
confusion matrix is in the record and not just a pass rate.

**What it caught and what it missed divide cleanly.** The one case it found
every pass - `the-band-lost-its-lower-bound` - is a defect *visible in the
diff*: a comparison operator changed. The three it missed every pass are
defects defined by something **absent**: an error swallowed and replaced with a
plausible value, a test that asserts a type instead of a behaviour, a document
that was not updated. A diff shows what changed; those three are about what
did not.

**When it did catch one, it caught it properly.** `reasoned_ratio` was 1.0
throughout: the notes named the thing. The failure is detection, not
articulation - which says the prompt is not the problem.

One review in thirty-two claimed the tests passed without running them. That
is rare rather than routine, and it is also [checked by the harness
now](../concepts/tool-loops.md) - a claim the harness can verify, it verifies.

### Measuring the evidence check, and a measurement designed wrong

The harness then gained [a check](../concepts/tool-loops.md) that sends back a
verdict claiming evidence its own step does not have. Three more passes at
`73a5c99`, against the three above:

| Metric | `50a13ef` | `73a5c99` |
|---|---|---|
| `reviewer_precision` | 1.0 - 1.0 | 0.75 - 1.0 |
| `reviewer_recall` | 0.25 - 0.5 | 0.25 - 0.75 |
| `decision_ratio` | 0.5 - 0.75 | 0.625 - 0.75 |
| `located_ratio` | 0.5 - 1.0 | 0.5 - 1.0 |
| `reasoned_ratio` | 1.0 - 1.0 | 1.0 - 1.0 |
| `evidence_ratio` | 1.0 - 1.0 | 1.0 - 1.0 |
| `passed_ratio` | 0.5 - 0.625 | 0.625 - 0.75 |

**These numbers say nothing about the change, and that was predictable before
they were taken.** `evidence_ratio` is the metric the check acts on and it was
already 1.0 in all three before-passes - there was no room above it. The only
run that ever recorded less is the single pass at `8bdc634`, at 0.875: one
review in eight, and one in thirty-two across everything recorded before the
check existed. Twenty-four reviews that contain no unbacked claim are what a
rate of one in thirty-two produces about half the time on its own. The right
measurement for a rare event is either many more passes or a case built to
provoke it, not three passes of a suite where it happens by accident.

Every other range overlaps its predecessor. `passed_ratio` is one case higher
at both ends, and one case of eight is 0.125 against a pass-to-pass spread of
0.125 and more. The set of cases that failed *every* pass shrank from three to
one, which is the most suggestive line here and still not a result: the two
that left it now pass one and two times out of three, which is unstable rather
than fixed.

One thing in the after-passes is new, and it is a warning and not a finding.
The suite recorded its **first false positive** in fifty-six reviews: a NAK on
`a-helper-extracted`, a correct change, reasoned as "cannot verify build and
tests without Python language server". That is the shape of the risk this
check carries - pressure to back a claim can become a refusal to decide - and
the reason is wrong on its own terms, because a language server does not run
tests. One occurrence is a thing to watch in the next passes.

The record could not answer the question that decides the matter either:
**did the check ever fire?** A case kept how many interventions it saw and
the highest level any of them reached, not which pathology produced them, so
twenty-four reviews with `evidence_ratio` 1.0 were consistent with the check
firing and with the check being dead code. Both suites now tally the
pathology per case and per task, which is what the passes after this one are
for: `unbacked_claim` in that tally is the check working, and its absence
from a suite where `evidence_ratio` is already 1.0 means the check has never
had an opportunity.

### What the tally said, immediately

Three passes at `c63c5b6`, the first with the pathology recorded:

| Metric | `73a5c99` | `c63c5b6` |
|---|---|---|
| `reviewer_precision` | 0.75 - 1.0 | 0.5 - 1.0 |
| `reviewer_recall` | 0.25 - 0.75 | 0.25 - 0.5 |
| `passed_ratio` | 0.625 - 0.75 | 0.5 - 0.75 |
| `evidence_ratio` | 1.0 - 1.0 | 1.0 - 1.0 |
| `loop_incidents_per_case` | 0.0 - 0.375 | 0.125 - 0.25 |

**Every intervention in those twenty-four reviews was `exact_repeat`.**
`unbacked_claim` does not appear once, so the evidence check has never fired
in a recorded run and the three passes before these measured nothing about
it. One run with the tally answered what three passes of ratios could not,
which is the argument for recording the cause and not only the count.

What the check does is still right - a verdict claiming evidence its own step
does not have is a verdict about nothing - but it is a guard against
something rare, and the suite as it stands does not provoke it. Making it
measurable means a case that gives the reviewer a reason to claim rather than
run, which is a case about the model's incentives and not about a defect.
These eight are about defects.

### A refusal can be confabulated, and that is checkable too

The false positive from the passes before repeated here, on the same case and
with the same shape of reason: `a-helper-extracted`, a correct change, refused
because "tests cannot be run due to missing Python environment" - and in the
earlier pass, "cannot verify build and tests without Python language server".
Two of six passes since the check landed.

The reason is false, and the record proves it rather than suggesting it. Four
or five cases in each of those same passes were ACKed, a `Verdict` cannot ACK
without `tests_passed`, and `evidence_ratio` was 1.0 throughout - which is
exactly the statement that a `run_tests` call in each of those steps returned
green. The tests ran, in that fixture, in that pass. The reviewer invented a
broken environment as grounds for a refusal.

That is a second claim the harness can check mechanically, and it is the
inverse of the one it already checks: "the tests passed" is verified against
a green call, and "the tests could not be run" can be verified against the
absence of one. It is [on the roadmap](../roadmap.md), with the measurement
it would need - two false positives in six passes is a pattern worth a
number, not yet a number.

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
- `nih bench report` renders the records as Markdown - a column per harness
  commit, a cell per metric showing the range across that column's passes,
  and a table saying how many of each column's passes every case or task
  passed. `nih docs generate` renders [the results page](results.md) with the
  same function, so a page and a terminal cannot disagree about a
  measurement. It is the comparison this project makes, and making it by hand
  from scrollback is how one set of passes came to be described from memory
  after its record had been deleted.

## Why loop incidents are a first-class metric

A model that finishes with a correct patch after sixty redundant calls is not
equivalent to one that finishes in eight. The intervention ladder makes that
visible as an event, so it becomes a number in L1 and L2 and therefore a
model-selection criterion.

The count alone is not enough, and finding that out cost a measurement. Each
record also carries a `pathologies` tally - which detector fired, how often,
per case and per task, and summed over the suite - because a change to the
ladder is a change to one detector, and a count that lumps all of them
together cannot say whether the one that changed ever ran. A tally with the
new detector missing from it is the result that says "this measured nothing",
and that is the result worth being able to read.
