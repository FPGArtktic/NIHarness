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

Three passes of the identical 14 tasks, nothing changed between them:

| Metric | Range across 3 passes |
|---|---|
| `passed_ratio` | 0.643 - 0.857 |
| `correct_sequence_ratio` | 0.857 - 1.0 |
| `correct_arguments_ratio` | 0.857 - 1.0 |
| `loop_incidents_per_task` | 0.214 - 0.571 |
| `effective_ratio` | **0.929 - 0.929** |
| `complete_ratio` | **0.929 - 0.929** |

The two that did not move are the two that ask about **effect** and **end
state**; everything measuring the route varies by up to three tasks in
fourteen. And both of the failures that repeated in all three passes were
caught *only* by those two.

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
