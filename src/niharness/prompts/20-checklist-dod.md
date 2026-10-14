<!--
SPDX-FileCopyrightText: 2026 Mateusz Okulanis
SPDX-License-Identifier: GPL-3.0-or-later
-->

# Definition of Done

Fill this checklist in your final answer. Each item is `done` only if you can
point at evidence you actually observed - a command you ran and its output.
An unfilled or optimistic checklist fails the step.

- [ ] The task's intent is implemented, and nothing beyond it.
- [ ] Every acceptance criterion the task claims is satisfied, named one by one.
- [ ] The change is one logical change, in one commit, with a message that says
      why.
- [ ] `run_lint` is clean on the changed code.
- [ ] `run_build` succeeded.
- [ ] `run_tests` succeeded, and at least one test exercises the new behaviour.
- [ ] No test was weakened, skipped or deleted to get there.
- [ ] No dependency was added, or the request is written in `NOTES.md`.
- [ ] Documentation, schemas and generated files that this change invalidates
      were regenerated in the same commit.
- [ ] Anything left undone, uncertain or blocked is written in `NOTES.md`.
