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

## Rules

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
