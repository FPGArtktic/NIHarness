<!--
SPDX-FileCopyrightText: 2026 Mateusz Okulanis
SPDX-License-Identifier: GPL-3.0-or-later
-->

# 0011. Message assembly is an application concern

- Status: Accepted
- Date: 2026-10-14

## Context and problem statement

The domain had a `build_messages(items)` that turned window items into a
message list by sorting them by priority. It was tested and it was wrong in two
ways that only appear once a real step runs:

- A tool message must follow the assistant message that requested it, and must
  carry that call's id. Sorting by priority separates them, and a priority-
  ordered history is not a conversation.
- An assistant message that proposed a call has to carry `tool_calls` on the
  wire. A `ContextItem` holds text, so the calls were lost.

Both are rules of the protocol the harness speaks, not of the harness's own
policy.

## Decision drivers

- The domain decides *what* the window keeps; it should not know what a tool
  message is required to look like.
- Compaction decisions and wire format must stay in step: an item that is
  digested must change the message that carries it.
- Token counts must come from the tokenizer, and the policy is pure and
  synchronous while the tokenizer is a port and asynchronous.

## Considered options

1. **Pair each window item with the message that carries it, in
   `niharness.application.Window`, and delete `build_messages` from the
   domain.**
2. Extend `ContextItem` with tool-call fields and fix the ordering in the
   domain.
3. Keep both: a domain assembler for simple cases and a real one in the
   application layer.

## Decision outcome

Option 1. `Window` holds `(item, message)` pairs: the item is what the policy
reasons about, the message is what goes on the wire. Applying a plan keeps them
together - a digested item rewrites its message's content, a dropped item takes
its message with it - and order is insertion order, which is chronological.

Measurement is handled by pre-measuring. Everything the policy can ask to
measure - every item's content, and the digest line that could replace it - is
measured through the `Tokenizer` port before planning, and the policy is handed
a synchronous lookup. A miss raises `MeasurementMissingError` rather than
returning an estimate, because estimating a token count is the one thing this
project refuses to do.

Option 2 would have put `tool_call_id` into the domain and made `messages.py`
and `context.py` import each other. Option 3 is two implementations of one
thing, which is how they drift.

## Consequences

- `build_messages` is gone; its ordering tests moved to the window's tests,
  where they can assert the protocol rule they were really about.
- The compactor model is optional. Without one, folded-away turns are replaced
  by a sentence saying how many there were and that they remain in the event
  log - a stated gap instead of a silent one.
- Indexing compacted content into the vector store, so `rag_query` can recover
  it, is still ahead on the roadmap. Until then the event log is the only way
  back, and the window says so.
