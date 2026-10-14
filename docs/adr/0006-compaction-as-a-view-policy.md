<!--
SPDX-FileCopyrightText: 2026 Mateusz Okulanis
SPDX-License-Identifier: GPL-3.0-or-later
-->

# 0006. Compaction is a view policy, not a log edit

- Status: Accepted
- Date: 2026-10-14

## Context and problem statement

A 64k window fills up during a real task: file reads, build output, failed
patches. Something must go. The naive implementation drops or rewrites history -
and then the run can no longer be replayed, audited or benchmarked, and the
model can no longer recover what it was told an hour ago.

## Decision drivers

- The event log must stay complete and replayable.
- Priority-one context (prompt layers, the current task) must never be dropped,
  under any fill.
- Most compactions should not require a model call.
- Information must be recoverable after it leaves the window.

## Considered options

1. **The window is a derived view; compaction rewrites that view, and anything
   compacted away is indexed into the vector store.**
2. Truncate the message history in place.
3. Summarise everything with the model at every threshold crossing.

## Decision outcome

Option 1. `plan_compaction` is a pure function from window items plus a policy
to a new window plus an audit trail of decisions. Its passes run cheapest first:
dedupe identical results, drop superseded build/test/lint output, digest tool
results outside the keep window, then - only if still over target - fold older
turns into a `ContextSummary` produced by the utility model, and as a last
resort drop the lowest-priority items.

Everything removed is indexed into the run's collection, so `rag_query` brings
it back. A `context.compacted` event records before and after token counts and
the summary digest.

Token counts are measured, never estimated: the function takes a `measure`
callable backed by the backend tokenizer.

## Consequences

- Property-tested invariants: the window never grows, priority one and two
  survive byte for byte, the result fits or declares `overflow`, and the same
  window always yields the same plan.
- A changed compaction policy means extending the hypothesis invariants first.
- Digest lines keep identity (`read_file src/net.c: 412 lines, sha 3f9a...`),
  so the model knows what it has already seen.
- Retrieval becomes a dependency of long runs. Without the vector store,
  compacted context is gone from the window and only recoverable from the log by
  a human.
