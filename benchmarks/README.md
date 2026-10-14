<!--
SPDX-FileCopyrightText: 2026 Mateusz Okulanis
SPDX-License-Identifier: GPL-3.0-or-later
-->

# Benchmark suites

Four layers, described in `docs/benchmarks/methodology.md`. Task definitions live
under `suites/`, results under `results/` as JSONL, one object per line, tagged
with the harness commit, prompt version, model, quantization, profile and date.
Results are committed, so a regression is a diff.

```
suites/l0/   inference throughput: no tasks, a matrix of context sizes
suites/l1/   tool calling: 20 tasks, 3-5 calls each
suites/l2/   steps: gate and review against a golden set of verdicts
suites/l3/   end-to-end: 10 tasks per language, hidden tests
```

A task definition is TOML so it can be read and reviewed without running
anything. Vendored external sets (SWE-bench subset for L3, BFCL for L1) arrive
with their own license files and are listed in `THIRD_PARTY.md`.
