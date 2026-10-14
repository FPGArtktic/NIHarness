<!--
SPDX-FileCopyrightText: 2026 Mateusz Okulanis
SPDX-License-Identifier: GPL-3.0-or-later
-->

# Models

Models are **not** distributed with this repository. Download them yourself,
record the license you accepted, and point the configuration at the local
endpoint that serves them.

| Role group | Model | Serving | License to verify at download |
|---|---|---|---|
| plan, implement, review, fix, research | Qwen3-Coder-30B-A3B-Instruct, Q4_K_M | `llama-server` | Apache-2.0 |
| brainstorm, optional reasoning | Qwen3-30B-A3B-Thinking | `llama-server` | Apache-2.0 |
| summaries, compaction, routing | BitNet b1.58-2B-4T | `bitnet.cpp` | MIT |
| embeddings | bge-m3, or a BitNet embedding model on `cpu-only` | `llama-server` | MIT / Apache-2.0 |

<div class="refusal" markdown>
The licenses above are what upstream published when this page was written.
Verify them at download time; nothing in the harness checks them for you, and a
model license is not a dependency license.
</div>

## Why the utility model is ternary, and what else fits

The utility model has one job and a hard constraint. The job: summaries,
`ContextSummary` for compaction, `NOTES.md` prose, routing and classification -
no tools, no reasoning about code. The constraint: it is **resident at the same
time** as the coder model and a sandbox that may be running a compiler, so it
has to be small in RAM and cheap in CPU cycles, and it is called at every step
boundary, so its latency lands on the critical path.

Ternary (1.58-bit) weights are the cheapest way to meet that: a 2B model is
well under a gigabyte, and the kernels are integer additions rather than
floating-point multiplies, which is exactly what a CPU with no spare VRAM wants.
A post-quantized 2B model at Q4 is several times larger and slower on CPU for
the same job.

| Candidate | Why it is a candidate | What to check |
|---|---|---|
| **BitNet b1.58-2B-4T** (shipped default) | natively trained ternary, not post-quantized; MIT; `bitnet.cpp` is its reference CPU runtime, so the air-gapped host needs one binary and no CUDA | `bitnet.cpp`'s server does not offer GBNF, so the summary schema cannot be *enforced*, only validated after the fact |
| **Falcon-E / Falcon-Edge 1B and 3B** (TII) | also natively ternary on the BitNet architecture, released with bf16, BitNet and pre-quantized variants; TII reports Llama-ecosystem compatibility, so it can be served by `llama-server` | the license is a TII Falcon licence rather than plain Apache-2.0 - read the model card before downloading - and the pre-quantized variant is what makes later fine-tuning on this project's own summaries possible |

### How big should it be?

Bigger is cheap in memory and expensive in the only resource that is actually
scarce here. The numbers, for this model family at 1.58 bits:

| Model | Weights resident | What it costs on a CPU-bound host |
|---|---|---|
| Falcon-E-1B | ~0.3 GiB | the cheapest summary; the most likely to invent one |
| BitNet b1.58-2B-4T | ~0.6 GiB | the shipped default |
| Falcon-E-3B | ~0.9 GiB | roughly three times the prompt-processing work of the 1B |

None of those threaten a 32 GiB machine, let alone a 64 GiB one - next to about
18 GiB of coder weights they are a rounding error. The constraint is **cores**,
not bytes: on the `cpu-only` and `hybrid-small-vram` profiles the coder's
experts already run on the CPU, so every second the utility model spends
summarising is a second the coder is not generating, and compaction runs at
every step boundary. That is the whole argument for keeping this model small,
and it is why "just use a 7B Falcon for summaries" is the wrong trade: it is not
the gigabytes, it is the contention.

Going up *within* the ternary family is a different question, and a fair one.
The failure that matters for this role is not clumsy prose, it is **invention** -
a file that was never touched, named confidently, sending the next step to read
something that does not exist. Larger models invent less. So the rule is:

> Use the smallest model whose summaries are faithful under a grammar
> constraint; move up a size when the golden check says it invents, not because
> a larger number looks safer.

The harness does not have to buy that trust with parameters alone. A
`ContextSummary` claims which files were touched, and that claim is checkable:
`niharness.domain.artifacts.ungrounded_paths` compares it against the text the
model was given, and the summarizer drops any path the transcript never
mentioned, logging what it dropped. The model's word is not evidence here
either.

The argument that actually decides it is structured output. The utility model's
only deliverable is a `ContextSummary` that validates; the harness already
tolerates a model that answers prose, by replacing the folded-away turns with a
pointer to the event log, but that is a degraded window. A model served by
`llama-server` can be constrained with a GBNF grammar, which turns "usually
valid JSON" into "valid JSON by construction". That is a point in Falcon-E's
favour, and the reason the profile carries `supports_grammar` per model rather
than per backend:

```toml
[models.utility]
name = "Falcon-E-1B-Instruct"
base_url = "http://127.0.0.1:8081"   # llama-server, not bitnet.cpp
context_window = 8192
supports_grammar = true
quantization = "bitnet-1.58"
```

Swapping is a configuration edit, not a code change: the port is
OpenAI-compatible HTTP and nothing in the harness knows which runtime answers.
Which one is better here is an `nih bench --layer L0` run plus a golden check on
the compactor prompt, not a matter of taste - see
[the benchmark methodology](../benchmarks/methodology.md).

## What each configured model is for

The profile names up to four models. They are separate processes on separate
ports, and the harness picks between them by role, never by guessing:

| Section | Who uses it | What it does | Where it runs |
|---|---|---|---|
| `[models.main]` | planner, implementer, reviewer, fixer, researcher | all the work on code: reading, patching, reviewing, running the gate through tools | GPU, CPU, or split, per profile |
| `[models.thinking]` | brainstormer; optionally planner and reviewer | the reasoning variant, for open-ended exploration before anything is written | same server as the main model; a swap is allowed only at a stage boundary |
| `[models.utility]` | compactor, NOTES.md, routing, classification | summarises what no longer fits in the window; never gets tools | CPU, always resident |
| `[models.embeddings]` | the vector store | embeds code chunks and compacted run context for `rag_query` | GPU on the GPU profiles, CPU otherwise |

One run uses **one** main model throughout: the roles differ by prompt,
allowlist and temperature, not by weights, so there are no model swaps inside a
tool loop.

`[models.thinking]` is optional, and `cpu-only` leaves it out on purpose.
Reasoning is generated tokens, and generation is what a CPU is slowest at: a
thinking variant emits hundreds to thousands of tokens before it answers, which
on that profile is minutes per turn, on top of a second set of roughly 18 GiB of
weights. Where it is absent, the brainstormer runs on the main model with a
higher temperature - a worse brainstorm, but one that answers this side of
lunch.

## Rules

- **Quantization floor: Q4_K_M.** Below that, tool-call syntax degrades before
  prose does, which is the worst failure mode for a harness.
- **Context: 64k** on the hybrid and GPU profiles, 32k on `cpu-only`.
- **One main model per run.** Roles differ by prompt and permission. A swap
  between Thinking and Instruct happens only at a stage boundary, never inside
  a tool loop.
- **Before a model is given tools**, it must pass `benchmarks/suites/L1` - 20
  tasks, 3-5 tool calls each. A model that cannot call tools reliably does not
  get a tool allowlist.

## Serving

Both backends speak OpenAI-compatible HTTP, which is the only protocol the
harness knows:

```bash
llama-server -m Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf \
  --host 127.0.0.1 --port 8080 --ctx-size 65536 --n-gpu-layers 0 \
  --cache-type-k q8_0 --cache-type-v q8_0

# utility model, always resident
bitnet-server -m BitNet-b1.58-2B-4T.gguf --host 127.0.0.1 --port 8081
```

A non-localhost URL is refused when the client is constructed - see
[ADR 0004](../adr/0004-openai-compatible-inference-port.md).
