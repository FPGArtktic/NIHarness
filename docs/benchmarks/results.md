<!--
SPDX-FileCopyrightText: 2026 Mateusz Okulanis
SPDX-License-Identifier: GPL-3.0-or-later
-->

# Benchmark results

No results yet: the benchmark runner is still ahead on
[the roadmap](../roadmap.md), and this project does not publish numbers it has
not measured.

When records exist, this page is generated from `benchmarks/results/` by
`nih docs generate`: an SVG chart per layer with the raw numbers in a table
underneath, grouped by model, quantization and profile, plus a regression table
against the previous harness commit.

The record format is one JSON object per line:

```json
{"layer":"L1","harness_commit":"<sha>","prompt_version":"<sha>",
 "model":"Qwen3-Coder-30B-A3B-Instruct","quantization":"Q4_K_M",
 "profile":"hybrid-small-vram","date":"2026-10-14",
 "metrics":{"valid_calls":0.0,"correct_sequences":0.0,"loop_incidents_per_task":0.0}}
```

Records are committed, so a regression is a diff.
