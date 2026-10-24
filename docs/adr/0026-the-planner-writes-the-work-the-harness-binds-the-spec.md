<!--
SPDX-FileCopyrightText: 2026 Mateusz Okulanis
SPDX-License-Identifier: GPL-3.0-or-later
-->

# 0026. The planner writes the work; the harness binds it to the spec

- Status: Accepted
- Date: 2026-10-24

## Context and problem statement

The planning stage turns an accepted `SPEC.md` into a `Plan`. The `Plan`
artifact carries `spec_digest`, and for good reason: it is what makes a plan
traceable to the thing it was planned from, and what lets `nih implement`
refuse a plan whose spec has since changed.

But the planning stage is a model writing an artifact against a JSON Schema,
and that schema would then require the model to reproduce a sixteen-character
digest of a document it was shown as prose. It is the one field in the artifact
that is not a judgement: there is exactly one correct value, the harness
already knows it, and the model can only copy it.

A plan rejected because the copy slipped is a wall with no door in front of a
model that did the planning correctly. This project has removed three of those
walls in a week - a refusal that reported a hash instead of its reason, a diff
refused for line numbers the harness could strip, a creation that had to be
expressed as an edit with nothing to find. This is the same shape.

## Decision drivers

- A model should be asked for judgement and never for transcription.
- The traceability `spec_digest` provides is not negotiable: dropping the field
  would make a plan untraceable, which is worse than asking for it.
- Artifacts are the only thing that crosses a stage boundary (§4), so whatever
  is written has to be complete; a "mostly filled in" plan is not an artifact.
- The schema sent to the model and the schema the harness validates against
  should be the same object, not two that happen to agree.

## Considered options

1. **Ask for a `Plan`, overwrite the digest.** Smallest change. The schema
   keeps a required field whose value is discarded, which is a lie told to the
   model at a cost of tokens, and the next reader of the code cannot tell
   whether the field matters.
2. **Ask for a `Plan`, validate the digest, fail on a mismatch.** Honest, and
   the failure is pure paperwork. Measured elsewhere in this project: a step
   that fails for paperwork is a step that fails.
3. **Put the digest in the prompt and ask the model to copy it.** Same as 2
   with extra steps, and sixty-four characters of window per attempt.
4. **A separate artifact for what the planner writes.** `PlanDraft` carries
   the tasks and the open questions; the harness binds it to the spec's digest
   to produce the `Plan`. The schema the model sees contains only decisions.

## Decision outcome

**Option 4.** `PlanDraft` is what the planner produces; `PlanDraft.for_spec()`
is what makes it a `Plan`.

Two details matter more than the split itself:

- **Every rule about the tasks stays on `Plan`.** Unique ids, dependencies
  that point backwards only, at least one task - `PlanDraft` does not restate
  any of them, so binding is where they fire. A draft that validates and still
  cannot become a plan is a planning error, and it is reported as one, with
  the offending id named, rather than as a schema failure.
- **The spec never enters the window twice.** The planning step renders the
  spec for the model itself rather than reusing the `SPEC.md` renderer, which
  repeats the whole artifact as a JSON block for the gate's benefit. The
  window gets the content once, and the digest not at all.

The spec gate's schema half runs before any model is started: a spec whose
acceptance criteria are not testable is rejected offline, by the `Spec` model's
own validator. The reviewer half - a one-shot ACK/NAK pass on the spec - does
not exist yet, and the `spec.accepted` event says which half ran (`reviewed`)
rather than implying a review nobody performed.

## Consequences

- One more artifact in `schemas/`, and one more `ArtifactKind`.
- `Plan.spec_digest` can be trusted as a harness fact wherever it is read.
- The planner's prompt no longer mentions a `Questions` artifact, because the
  loop can enforce exactly one final artifact per step and there is no
  `questions.asked` producer yet. Open questions go into the draft's
  `open_questions`, which the human reads before approving - the point at
  which an assumption is cheapest to correct.
- The same argument applies to any future artifact with a field the harness
  already knows. A model is asked what it decided, never what it was told.
