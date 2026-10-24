<!--
SPDX-FileCopyrightText: 2026 Mateusz Okulanis
SPDX-License-Identifier: GPL-3.0-or-later
-->

# 0031. A brainstorm session is many steps over one window

- Status: Accepted
- Date: 2026-10-24

## Context and problem statement

Every stage in this harness is one step: a window is built from the prompt
layers and an artifact, a model answers, the answer is validated, the step
ends. Entry A is not that shape. Brainstorming is a conversation - a human
says something, the model reads code and answers, and that repeats until
there is a spec worth gating.

Three things had to be decided, and the stub said so for months: "an
interactive session loop and a TUI pane for the running draft; the model, the
read-only tools and the prompt layer are built".

**What a turn is.** A whole session could be one step with one enormous
budget, the human's words arriving as instructions inside it. Then the
detectors, the intervention ladder and the budget apply to the *session*
rather than to a turn, so a model that loops on `read_file` for twenty calls
has not tripped anything until the session is nearly over.

**Where the budget lives.** §4 gives each step a budget. If each turn is a
step with the stage's budget, a conversation never ends; if each turn gets a
share, the share is a guess about how many turns a human will take.

**Who says who wrote the spec.** `Spec.origin` is one of three enum values.
The harness knows which entry point is running - `nih brainstorm` is entry A
by construction - and a model can only transcribe it.

## Decision

**A session is a sequence of steps sharing one window.** Each human turn is a
step with its own `StepId`, so the detectors, the ladder and the stop
conditions apply per turn exactly as they do to implementing work. The window
is not rebuilt between them: the conversation is the window, and §4's rule is
that history never crosses a *stage* boundary, not that a stage may call a
model once.

**The budget is the stage's, spent across the session.** `BrainstormSession`
subtracts each turn's spend and hands the next turn what is left, so §4's
budget means what it says and a conversation cannot outrun it by being a
conversation. The closing turn is exempt from exhaustion: a session that spent
its budget and then cannot be asked for the spec spent it on nothing.

**The brainstormer writes a `SpecDraft` and the harness binds the origin**,
which is the third application of ADR 0026's rule - the planner's
`spec_digest`, the implementer's commit, and now this. Every rule `Spec`
carries is applied at the moment of binding rather than relaxed.

**The closing turn says what it is for.** It runs with `max_tool_calls` at
zero, which is the existing idiom for a no-tool step and makes the loop attach
the artifact's grammar. The shipped finalisation instruction tells a model its
tool budget is spent and to write its state into `NOTES.md`, which is right
for a step that was cut off and wrong for one whose only turn was always going
to be the artifact - so `LoopConfig.closing_instruction` lets the caller say
which it is. That is the same collision as the reflection turn's, found the
same way, and fixed before it could be measured.

## Consequences

`nih brainstorm` is a command before it is a pane. §9 describes the session in
the TUI with the running draft visible; the TUI reads the event log and writes
the control inbox and does not type for the human, so the session has to exist
as a command and the pane is a view of it. All three entry points now work.

The human's side of the conversation is a new event, `brainstorm.said`, kept
verbatim. The model's side is a `model.response` with a digest of its content,
because §15 keeps unvalidated model output out of the log; the human's words
have no such problem and are the only record of what was asked.

A step that demands no artifact now keeps the prose it ended on, in
`LoopState.final_text`. `final_artifact=None` documented that "any prose
answer does" and gave the caller no way to read it.

`/plan` does not run the planner. A stage boundary starts a fresh run seeded
only by the artifact, the artifact is on disk by then, and the next stage is a
command the human runs - which is also what keeps a spec reviewable before
work is planned from it.
