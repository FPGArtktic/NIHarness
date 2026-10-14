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

## 6. Execute the plan

```bash
uv run nih implement                      # the approved plan
uv run nih implement --plan PLAN.md       # a specific one
```

It creates a worktree on `harness/run-<id>`, opens that run's event log, starts
the tool server in the sandbox, and runs one implementing step per task:

```text
run 20261014-213005-ab12cd
  worktree: /project/.nih/worktrees/20261014-213005-ab12cd (harness/run-...)
  model: Qwen3-Coder-30B-A3B-Instruct at http://127.0.0.1:8080
  tools: sandbox
  tasks: 3

  [ok  ] t1: final_answer, 11 tool call(s), 48213 tokens
  [FAIL] t2: budget_tool_calls, 40 tool call(s), 141002 tokens

run 20261014-213005-ab12cd: stopped at t2
```

A task that does not finish stops the run: the next task would build on work
that is not there. The event log has everything that happened, and
`nih status <run-id>` folds it.

Two things have to be running first: the model (`llama-server` on the configured
port) and the sandbox image. Without the image the command says so and exits 69.
On a machine without Podman you can run the tools on the host instead:

```bash
uv run nih implement --unsafe-local-tools
```

The name is the warning: the tool server then runs outside the container, where
it can reach the network and whatever your user can read. The worktree is still
the only path it writes to, and the gate still runs - and the run's
`run.started` event records that the tools were local, so a run made that way
cannot be mistaken for a sandboxed one later.

## What comes next

Review and fix are not wired up yet: the loop ends a step on a `FinalAnswer`,
and the reviewer's artifact is a `Verdict`. [The roadmap](../roadmap.md) has the
order of work.
