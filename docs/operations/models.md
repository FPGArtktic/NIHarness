<!--
SPDX-FileCopyrightText: 2026 Mateusz Okulanis
SPDX-License-Identifier: GPL-3.0-or-later
-->

# Models

Models are **not** distributed with this repository. Download them yourself,
record the license you accepted, and point the configuration at the local
endpoint that serves them.

## What has actually been run

Every record in `benchmarks/results/` names one family. The configuration's
defaults are something else, and naming them as though they were in use would
be describing an intention in the present tense:

| Model | Used for | Serving | Measured | License to verify at download |
|---|---|---|---|---|
| Ternary Bonsai 2, 4B / 9B / 27B (PrismML) | every live run so far: plan execution, implement, review, fix | `llama-server`, **PrismML fork** | L0 throughput, L1 tool calling (4B) | Apache-2.0 |
| bge-m3 | embeddings for `nih ingest` and `rag_query` | any `/v1/embeddings` endpoint | used, not benchmarked | MIT |

The PrismML fork is not optional for those weights: `PQ2_0` and `PTQ1_0` are
its packings and upstream llama.cpp does not know the tensor types. Pick
`PQ2_0` - see the measurements below, where the denser `PTQ1_0` is 25x slower
at prompt processing on a CPU.

## What the configuration defaults to

Written into the profiles and never exercised. Here because the defaults are
real and someone will serve them, not because anything says they work:

| Section | Model | Serving | Status | License to verify at download |
|---|---|---|---|---|
| `[models.main]` | Qwen3-Coder-30B-A3B-Instruct, Q4_K_M | `llama-server` | no run, no record | Apache-2.0 |
| `[models.thinking]` | Qwen3-30B-A3B-Thinking | `llama-server` | the brainstormer is a stub, so unreachable | Apache-2.0 |
| `[models.utility]` | BitNet b1.58-2B-4T | `bitnet.cpp` | no run, no record | MIT |
| `[models.embeddings]` | a BitNet embedding model, on `cpu-only` | `llama-server` | no run; bge-m3 is what has been used | MIT |

## Vision, which is not wired up

There is no vision model in this harness today and no configuration section
for one. It belongs to the document tier: `nih ingest` chooses per page
between a text layer and OCR, and a third tier is needed for a page where
neither works - a schematic, a timing diagram, a table whose structure *is*
the information, which `tesseract` returns as a scatter of words in the wrong
order.

The per-page choice in `domain/documents.py` is where it goes, and the shape
of the decision is already there: a tier is tried, and its answer has to beat
the previous one to replace it. What is missing is the port, the adapter and
the policy for when a rendering is worth a vision model's tokens.
[The roadmap](../roadmap.md) tracks it. Any model served over an
OpenAI-compatible `/v1/chat/completions` with image content would fit the
existing inference port - Qwen3-VL and the Gemma 3 vision variants are the
obvious candidates, both Apache-2.0 and Gemma's own license respectively, and
neither has been tried here.

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

### Ternary beyond the utility slot

The paragraphs above treat ternary as a way to make a *small* model cheap. A
second use is the interesting one for this project, and it is not settled.

**Ternary Bonsai** (PrismML, announced 2026-04-16) is a family of natively
ternary models at 1.7B, 4B, 8B and 27B, weights constrained to `{-s, 0, +s}`
with one FP16 scale per 128 weights. The reported footprints are what make it
worth a look here: roughly 0.4 GB for the 1.7B, 1 GB for the 4B, 2 GB for the
8B and 4-6 GB for the 27B, and the published suite includes BFCLv3, HumanEval+
and IFEval - tool calling and instruction following, which is what this harness
actually needs from a model.

Why that matters more than a smaller utility model: **the main model is the
binding constraint.** Qwen3-Coder-30B-A3B at Q4_K_M is about 18 GiB, which is
why the `hybrid-small-vram` profile puts the experts on the CPU and keeps only
attention and the KV cache on the GPU. A 27B-class model at 4-6 GB that keeps
most of its FP16 quality would change that arithmetic completely - weights and
a 64k KV cache in the place where only the KV cache fits today.

!!! warning "The runtime is the catch, and it is decisive"

    The ternary format needs **PrismML's llama.cpp fork**; upstream llama.cpp
    has only the 1-bit variant merged, and tools that bundle a stock build
    cannot run the ternary files at all.

    For the harness that is *not* a code change - the inference port is
    OpenAI-compatible HTTP and a fork still ships `llama-server`, so it is a
    profile entry and nothing more (§2 of CLAUDE.md holds). It is, however, an
    air-gap supply-chain decision: the fork has to be built and vendored, or a
    prebuilt release trusted, and that belongs in the bundle with everything
    else.

What is not known, and matters here more than any published average:

- **quality at 64k.** The window this harness builds is 64k and compaction
  decides what stays in it; a model that degrades at long context fails at
  exactly the job it is being hired for, and no benchmark above reports it.
- **multi-file patch discipline.** HumanEval+ measures a function in a vacuum.
  What a run needs is a unified diff that applies, against a tree the model has
  only seen through tools.
- **tool-call formatting under this project's schemas.** BFCLv3 is the right
  family of test, and `benchmarks/suites/L1` is the local version of it.

The published numbers are vendor and third-party reports, measured on different
suites: do not compare them to each other, and do not compare them to the
numbers in `benchmarks/results/`.

So: a candidate, not a choice. The project's own rule decides it - a model
change without an L0-L3 run in the same change is incomplete (§11) - and this
is one of the first questions `nih bench` should be pointed at once it exists.
Sources: [the announcement](https://prismml.com/news/ternary-bonsai),
[the format documentation](https://docs.prismml.com/download/formats) and
[its llama.cpp notes](https://docs.prismml.com/run/llamacpp).

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
| `[models.embeddings]` | the vector store | embeds documents, code chunks and compacted run context for `rag_query` | GPU on the GPU profiles, CPU otherwise |

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

### Serving the embedding model

Any server that speaks `/v1/embeddings` will do - it is the same
OpenAI-compatible surface as inference, so there is no embedding-specific code
path in the harness:

```bash
llama-server --embeddings -m bge-m3-f16.gguf --host 127.0.0.1 --port 8082
```

Two numbers have to agree, and the harness checks them rather than trusting
them. `[vector] embedding_dimensions` is the width the collections are created
at; bge-m3 returns 1024, which is the default. A model of another width is
refused at startup by name and number:

```text
bge-m3 returned 768-dimensional vectors, but the configuration declares 1024
(vector.embedding_dimensions). Fix the number, or point [models.embeddings] at
a model of that width.
```

An existing collection built at a different width is refused the same way,
naming the collection: the alternative is a wall of upserts that Qdrant refuses
one batch at a time.

Retrieval is optional. Where no embedding model is configured or served,
`rag_query` is left out of the model's tool list rather than offered and
broken - see [retrieval](../concepts/retrieval.md).

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
