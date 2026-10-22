---
name: niharness
description: Drive this project's NIHarness instance - write the plan, start a run, follow it, read the verdicts. Use when asked to implement something through the harness, to check or steer a run, or whenever `nih` or "the harness" comes up in this repository.
---

<!--
SPDX-FileCopyrightText: 2026 Mateusz Okulanis
SPDX-License-Identifier: GPL-3.0-or-later
-->

# Driving NIHarness

The harness runs local models that do the coding work. Your job is to decide,
not to do: write the spec and the plan, start the run, read digests, and merge
by hand. Reading files, writing patches and running tests inside a run cost you
nothing, because you are not the one doing them.

## The loop

1. Write `SPEC.md` and `PLAN.md`. One task is one logical change and therefore
   one commit; dependencies point backwards only. `nih plan check PLAN.md --json`
   validates it offline.
2. `plan_submit` → `plan_approve` → `implement_start`. Over MCP those are tools;
   from a shell they are `nih plan import`, writing `.nih/approved-plan.md`, and
   `nih implement --detach`.
3. Poll `run_status`. When something needs explaining, `run_tasks` first, then
   `run_events(since=cursor)` - never the raw log.
4. On a handoff, read the diagnostics and decide: change the plan and start
   again, or hand it back to the human. The harness has already tried the fix
   loop.
5. Merging `harness/run-<id>` is a human action. Do not merge it for them.

## Rules

- **Do not do the work yourself.** If you find yourself reading project files to
  fix what a run could not, stop and ask whether the plan was wrong. That is
  usually the real answer, and it is the one that stays cheap.
- **Do not try to bypass the gate.** There is no tool for it here, and that is
  deliberate: a commit exists if and only if format, lint, build and the quick
  tests passed.
- **Report red output verbatim** when you summarise a failure. A paraphrased
  gate failure is worse than no summary.
- **One `run_status` per decision**, not per minute. A run takes as long as it
  takes, and polling is not supervision.

## Commands that need no agent

`nih runs`, `nih status <id>`, `nih cancel <id>`, `nih gate --tier quick`,
`nih-tui --run .nih/runs/<id>`. Everything you can do, a person can do without
you; the surface is one implementation.

Details: `docs/operations/managing-from-claude.md`, and `docs/orientation.md`
before changing the harness itself.
