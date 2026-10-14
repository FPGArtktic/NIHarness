<!--
SPDX-FileCopyrightText: 2026 Mateusz Okulanis
SPDX-License-Identifier: GPL-3.0-or-later
-->

# Pipeline

Three entry points, one pipeline. The harness does not care whether a human, a
brainstorming session or another agent wrote the spec: everything passes the
same gates.

```mermaid
stateDiagram-v2
  [*] --> brainstorm: nih brainstorm
  [*] --> spec_gate: SPEC.md brought in
  [*] --> plan_gate: PLAN.md brought in
  brainstorm --> spec_gate: SPEC.md
  spec_gate --> plan: accepted (fresh run)
  spec_gate --> handoff: rejected
  plan --> plan_gate: PLAN.md
  plan --> plan: questions
  plan_gate --> approval: valid
  plan_gate --> handoff: invalid
  approval --> implement: approved (fresh run)
  approval --> handoff: rejected
  implement --> commit_gate: work reported done
  commit_gate --> review: green
  commit_gate --> fix: red
  review --> full_gate: ACK
  review --> fix: NAK
  fix --> commit_gate: work reported done
  full_gate --> completed: green
  full_gate --> fix: red
  fix --> handoff: fix-loop limit
  completed --> [*]
  handoff --> [*]
```

## Entry points

| Entry | Command | What it assumes |
|---|---|---|
| A | `nih brainstorm` | nothing; the model explores the codebase with you and drafts `SPEC.md` |
| B | `nih plan SPEC.md` | you (or another agent) wrote the spec |
| C | `nih plan import plan.md`, then `nih implement --plan PLAN.md` | you brought a finished plan and only want it executed |

## Fresh runs at stage boundaries

Two transitions start a **new run**, seeded only with an artifact:

- spec accepted → `plan`, seeded with `SPEC.md`
- plan approved → `implement`, seeded with `PLAN.md`

Conversation history never crosses those boundaries. This is not an
optimisation, it is the forcing function: if a decision is not written into the
artifact, it does not survive, and the next stage cannot silently depend on
something it was told in passing.

## One task, one commit

A task is one logical change. The implementer works in a worktree on
`harness/run-<id>`, commits through [the gate](gate.md), and the reviewer then
re-runs the build and the tests itself - it does not trust the implementer's
claims. A `NAK` or a red gate sends the task to `fix`, bounded by
`max_revisions` (three by default); exceeding the bound hands the task back to
the human with the loop diagnostics in `NOTES.md`.

## Budgets

Every step carries a tool-call, token and wall-time budget, enforced by the
harness rather than requested from the model. Two calls before the tool budget
ends, the next model call is made with `tool_choice: none`, so a step finishes
with a written state instead of a severed tool call.

See the generated [configuration reference](../reference/config.md) for the
budget and policy keys, and [ADR 0005](../adr/0005-commit-is-the-gate.md) for
why the commit is the gate.
