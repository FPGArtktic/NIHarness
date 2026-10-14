<!--
SPDX-FileCopyrightText: 2026 Mateusz Okulanis
SPDX-License-Identifier: GPL-3.0-or-later
-->

# Prompts

A prompt is assembled from layers, and layers are versioned source:

```text
00-policy.md              immutable: coding standard, commit rules, prohibitions
10-role-<role>.md         what this role does and refuses to do
<task from PLAN.md>       the current task
<retrieved context>       chunks from the vector store
20-checklist-dod.md       the Definition of Done, filled by the model
```

The policy layer is modelled on the kernel's `SubmittingPatches`: one logical
change per commit, the message says *why*, untested code is not submitted, style
is the linter's job, and when you are uncertain you stop and write to
`NOTES.md`.

## Roles

| Role | Model | Tools | Structured output |
|---|---|---|---|
| brainstormer | thinking variant, warm | read-only | - |
| planner | coder | read-only | `Plan` |
| implementer | coder | read-only + patch, lint, build, test, commit | `FinalAnswer` |
| reviewer | coder, temperature 0 | read-only + lint, build, test | `Verdict` |
| fixer | coder | same as implementer | `FinalAnswer` |
| researcher | coder | read-only | - |
| compactor | utility (ternary, CPU) | none | `ContextSummary` |

Roles differ by **prompt and permission**, not by weights: one main model serves
a whole run, so there are no model swaps inside a run. A swap between the
Thinking and Instruct variant is allowed only at a stage boundary.

`shell` is in no default allowlist. The reviewer cannot patch or commit; the
brainstormer, planner and researcher cannot change anything at all.

## Reasoning is logged, never fed back

`reasoning_content` goes to the event stream and the TUI. It is not put back
into the message history - and there is no field on a `Message` that could carry
it, so the rule is enforced by the type, not by discipline.

## Golden tests

Each role's assembled prompt is pinned by a snapshot under `tests/golden/`. A
prompt change is therefore a commit that also updates the snapshot, with the
observed behaviour change stated in the commit body, and it is bisectable later.

```bash
uv run python -m tests.golden.regenerate   # deliberate, part of the same commit
uv run pytest -m golden
```
