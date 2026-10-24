<!--
SPDX-FileCopyrightText: 2026 Mateusz Okulanis
SPDX-License-Identifier: GPL-3.0-or-later
-->

# 0029. The harness shortens a commit subject it can shorten

- Status: Accepted
- Date: 2026-10-24

## Context and problem statement

`git_commit` is the gate, and it validates the message first: a Conventional
Commits subject, within a length limit, with a body that says why. Three rules,
all refusals.

The length rule was measured and it does not work. On a live run a 4B was told
the 72-character limit three times and answered 99, then 78, then 78
characters, with everything else about the step green. The response was to
explain better: give the overshoot as a number rather than restating the limit,
and offer a subject that fits. That is the state ADR 0022 would predict - the
harness computing the answer and then asking the model to apply it.

Three L3 passes settled it. `committed_ratio` **0.0**, with the
`commit_message` rule refusing **two, six and six** calls per pass, and the
refusal's own shortened subject handed over verbatim each time and re-sent
over-long anyway. `exact_repeat` fired nine to fourteen times a pass, mostly on
this. The gate never ran, so no work landed, on tasks whose working trees the
hidden tests accepted.

§15 already said what to do: *do not ask a model for bookkeeping it cannot do
when the harness can do it: hunk headers, line counts, character counts.* A
subject's length is a character count. The suggestion was a half-measure of
exactly the kind ADR 0022 removed from diffs.

## Decision

**The harness shortens an over-long subject itself**, and keeps every word the
model wrote. `shortened_subject` already produced a subject that fits - trimmed
at a word boundary, dangling words dropped, Conventional form preserved - so
that becomes the subject, and the model's original first line becomes the
body's first line, where there is no limit.

The repair is **reported, not silent**: the tool's result says what was
shortened and by how much, exactly as `apply_patch` reports a diff whose
clerical mistakes it repaired.

**The other two rules stay refusals**, because they are judgements the harness
cannot make. The Conventional Commits type says what kind of change this is.
The body says why it was made. Inventing either would be the harness writing
the commit's meaning rather than its formatting.

## Consequences

A commit subject in history may be the harness's shortening rather than the
model's sentence. That is the cost, and it is bounded: the full line is the
first thing in the body, so nothing the model wrote is lost, and `git log
--oneline` reads better for it.

A refusal that names the way out is still not a way out if the model cannot
walk it. This is the second time that has been the finding - the first was the
repeat message, where naming the refusal again beat handing back a digest - and
the rule it suggests is narrower than "explain better": when the harness can
compute the whole answer, explaining it is not a kindness but a test the model
has already failed.

The length rule is no longer a refusal, so the message explaining the overshoot
is deleted rather than left for a path that cannot reach it.

Measured by L3, where `committed_ratio` 0.0 across three passes with six
refusals apiece is the before.
