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
