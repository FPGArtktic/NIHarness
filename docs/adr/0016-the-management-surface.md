<!--
SPDX-FileCopyrightText: 2026 Mateusz Okulanis
SPDX-License-Identifier: GPL-3.0-or-later
-->

# 0016. The management surface: full control, no code path

- Status: Accepted
- Date: 2026-10-22

## Context and problem statement

The harness exists so that local models do the coding work. If a capable agent
supervises a run, the obvious question is what it may do - and the tempting
answer, "whatever helps", destroys the reason for the harness. An agent that
can read files and apply patches will read files and apply patches, and then
the expensive model is doing the work the cheap resident one was there to do.

## Decision drivers

- The supervisor's tokens should buy decisions, not transcripts.
- The gate must remain the only way a change becomes a commit, whoever is
  driving.
- A person without an agent must keep the same powers, through the TUI and the
  CLI.

## Considered options

1. **Read and steer: runs, plans, status, verdicts, diffs, cancel, approve,
   answer - and nothing that writes code or commits.**
2. The same plus `read_file` and `apply_patch`, so the supervisor can intervene
   directly.
3. Read-only supervision, with a person doing everything that changes state.

## Decision outcome

Option 1, exposed three ways over one implementation: `nih mcp` for an agent,
`--json` on the CLI for scripts, and the TUI for a person.

Absent on purpose, with a test asserting their absence: `read_file`,
`apply_patch`, `git_commit`, and any way to run the gate. The one call that
returns source is `run_diff`, and it returns what the run itself produced - the
change under review, not the project. A supervisor that genuinely needs to read
the repository has its own tools and can use them knowingly, outside this
surface.

Option 2 was rejected because it would make the harness a worse editor rather
than a better harness. Option 3 was rejected by the user: an agent that cannot
start or stop a run cannot supervise one.

## Consequences

- Every answer is a budgeted digest built from the primitives already used to
  keep tool results small, and says when it truncated. A ten-task run should
  cost a supervisor single-digit thousands of tokens.
- The surface needs a layer of its own: `niharness.management` holds the
  registry, the plan store and the views, with the CLI and the MCP server as
  two callers that do not know about each other. Two import-linter contracts
  hold that shape.
- A supervisor cannot rescue a stuck run by editing a file. It can cancel,
  change the plan, and start again - which is the workflow the pipeline already
  described.
