<!--
SPDX-FileCopyrightText: 2026 Mateusz Okulanis
SPDX-License-Identifier: GPL-3.0-or-later
-->

# Hardware profiles

A profile answers one question: where does the model run? `nih init` picks one
by probing the host; `harness.toml` overrides anything in it.

=== "cpu-only"

    No usable GPU. The coder model runs entirely on CPU, the utility model is
    the ternary BitNet build, embeddings come from a CPU embedding model.

    ```toml
    [models.main]
    context_window = 32768
    [compact]
    threshold = 0.65          # less headroom, compact earlier
    ```

=== "hybrid-small-vram"

    The common laptop case, and the default: 4-8 GiB of VRAM. Mixture-of-experts
    weights stay on the CPU, attention and the KV cache go to the GPU.

    ```toml
    [models.main]
    context_window = 65536
    [sandbox]
    cpus = 8.0
    ```

=== "gpu-full"

    20 GiB and up. The whole model is resident on the GPU; the utility model
    still runs on CPU so it never competes for VRAM.

## Sizing the context window

`nih init` does not pick 32k for a CPU host because CPU hosts are small. It
computes what fits, and on most machines that is the full window:

| Window | KV cache at q8_0 | Fits in |
|---|---|---|
| 16k | ~0.8 GiB | anything |
| 32k | ~1.5 GiB | a 4 GiB card, or 16 GiB of RAM with the weights |
| 64k | ~3.0 GiB | a 4 GiB card at the limit, or 32 GiB of RAM comfortably |

The arithmetic for Qwen3-Coder-30B-A3B: 48 layers x 1024 KV dimensions x 1 byte
per token at `q8_0` is about 48 MiB per 1024 tokens, so a 64k window costs
roughly 3 GiB. The Q4_K_M weights add about 18 GiB wherever the experts live.

Which budget applies depends on the profile:

- **`cpu-only`** - the KV cache lives in system RAM next to the weights, so the
  budget is `RAM - 18 GiB (weights) - 8 GiB (toolchain and build)`. With 64 GiB
  of RAM that leaves about 37 GiB, and a 64k window needs 3 GiB. **RAM is not
  what limits the window on a CPU host.** What limits it is prompt-processing
  time: re-processing a full 64k window on CPU costs minutes, which is why
  `cpu-only` compacts at 0.70 instead of 0.75 and keeps four verbatim tool
  results instead of six - the window is refilled less often, not made smaller.
- **`hybrid-small-vram` and `gpu-full`** - the KV cache lives in VRAM, so VRAM
  is the budget. A 4 GiB card runs a 64k window at `q8_0` with little room to
  spare; a 3 GiB card gets 32k.

64k is the *tested* maximum, not a hard limit. A larger window is a one-line
override, and nothing in the harness rejects it:

```toml
[models.main]
context_window = 131072

[compact]
context_window = 131072
```

Serve the model with a matching `--ctx-size`, keep `--cache-type-k q8_0
--cache-type-v q8_0`, and expect prompt processing - not memory - to be the
thing you notice.

## What a profile may set

Everything in the generated [configuration reference](../reference/config.md):
model endpoints and context windows, sandbox limits, the vector store URL, loop
thresholds and compaction policy.

## Measured, not guessed

`nih init` runs the L0 benchmark and writes the measured generation and
prompt-processing rates into the profile. Step time budgets derive from those
numbers; the harness does not guess a token rate, and a profile without measured
numbers is a profile with pessimistic budgets.

!!! note
    The L0 run needs a served model, so on a fresh machine `nih init` writes the
    configuration and reports the benchmark as outstanding.
