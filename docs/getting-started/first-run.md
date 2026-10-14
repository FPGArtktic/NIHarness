<!--
SPDX-FileCopyrightText: 2026 Mateusz Okulanis
SPDX-License-Identifier: GPL-3.0-or-later
-->

# First run

## 1. Probe the hardware

```bash
uv run nih init
```

`init` reads the CPU count, total RAM and (via `nvidia-smi`, if present) the
VRAM, picks a profile, and writes `harness.toml`:

| VRAM | Profile | Placement |
|---|---|---|
| 20 GiB and up | `gpu-full` | the whole model on the GPU |
| 3-20 GiB | `hybrid-small-vram` | experts on CPU, attention and KV cache on GPU |
| under 3 GiB | `cpu-only` | everything on CPU, ternary utility model |

It then lists what is still missing - the L0 benchmark, the sandbox image, the
Quadlet units - rather than pretending the harness is ready.

## 2. Look at the configuration

```bash
uv run nih config
```

Three layers are merged: shipped defaults, the profile, then `harness.toml`.
`NIH_`-prefixed environment variables override all three, nested with a double
underscore:

```bash
NIH_COMPACT__THRESHOLD=0.6 uv run nih config | grep -A3 compact
```

## 3. See what the gate will check

```bash
uv run nih gate --tier quick
uv run nih gate --tier full --changed docs/index.md
```

Languages come from marker files, so the plan is exactly what would run in the
sandbox.

## 4. Validate a plan, without a model

Entry point C needs no inference at all. A `PLAN.md` carries its artifact in a
fenced JSON block:

```markdown
# Plan

## 1. Add the parser

- id: `t1`

<!-- niharness:artifact -->

```json
{"schema_version":1,"spec_digest":"0123456789abcdef","tasks":[
  {"schema_version":1,"id":"t1","title":"Add the parser",
   "intent":"parse the artifact block","satisfies":[],"depends_on":[],
   "files":[],"commit_type":"feat"}],"open_questions":[]}
```

Then:

```bash
uv run nih plan check PLAN.md
uv run nih plan import external-plan.md -o PLAN.md
```

`plan check` *is* the plan gate: unique task ids, dependencies that point
backwards only, and a schema that validates.

## 5. Read a run's state

```bash
uv run nih status              # list runs
uv run nih status 8f21c0       # fold that run's event log
```

`status` is a fold over `events.jsonl` - the same fold the TUI uses, so the two
can never disagree.

## What comes next

Implementation needs the sandbox and the inference adapter. Until those land,
`nih implement` exits 69 and tells you so; [the roadmap](../roadmap.md) is the
order of work.
