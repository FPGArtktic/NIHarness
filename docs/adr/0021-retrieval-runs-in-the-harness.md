<!--
SPDX-FileCopyrightText: 2026 Mateusz Okulanis
SPDX-License-Identifier: GPL-3.0-or-later
-->

# 0021. Retrieval runs in the harness, over the store's REST API

- Status: Accepted
- Date: 2026-10-23

## Context and problem statement

`rag_query` has been in every role's allowlist since the roles were written,
and it answered nothing. Three things were missing: something to embed text,
somewhere to put the vectors, and a tool to ask. The first two are decided by
configuration the project already carries - bge-m3 and Qdrant - so what was
left to decide was where the tool runs and how the adapter talks to the store.

Where it runs is not a free choice. Every other tool a model may call runs
inside the sandbox container, and the container is started `--network=none`,
which is a non-negotiable (CLAUDE.md §1). The vector store is a service on the
host. A `rag_query` dispatched into the sandbox cannot reach it, and no amount
of configuration changes that without opening the container's network.

The second question is smaller but has the same shape as [ADR
0004](0004-openai-compatible-inference-port.md): every dependency has to be
mirrored into the wheelhouse for the air gap, and the harness already speaks
HTTP to localhost.

## Decision drivers

- `--network=none` on the sandbox is not optional, and is not negotiable for a
  convenience.
- A tool the model is told about must work. A `rag_query` that always answers
  "the store is down" is worse than one that was never offered, because the
  model spends calls on it.
- Retrieval needs nothing the sandbox exists to provide. It reads no file,
  touches no worktree and cannot write.
- Everything stays an event, validated by the domain first, truncated and
  budgeted - like every other tool.
- Dependencies cost wheelhouse space and an ADR each.
- Retrieval is optional. A project with no store running must still run.

## Considered options

1. **Serve `rag_query` from the harness process, routed by name, and talk to
   Qdrant over REST.**
2. Give the sandbox a network namespace reaching the store.
3. Run the store inside the sandbox, alongside the tools.
4. Use `qdrant-client` in the adapter instead of REST.
5. Proxy the store over the tool server's stdio connection.

## Decision outcome

**Option 1.**

`rag_query` is served by `adapters/retrieval`, a `LocalToolProvider`: the tool
port plus a statement of which names it answers.
`CompositeToolProvider` (ADR 0019) now routes over three destinations -
namespaced names to their declared server, the local provider's declared names
to it, and everything else to the sandbox. The names are declared rather than
discovered, so a local provider cannot claim a tool the sandbox holds; the gate
is in the container and stays there.

The adapter is `httpx` against five REST endpoints. Two ports were added:
`Embedder`, because an embedding model is not a chat model and the store should
be handed one without being handed the ability to generate; and the vector
store's failure vocabulary, which moved into `ports/vector_store.py` because a
caller has to tell "nothing matched" from "nothing is indexed" from "the store
is not running" without knowing what is behind the port.

Option 2 trades the sandbox's whole purpose for one read-only tool. Option 3
puts a service with persistent state inside a container that is created and
destroyed per command (ADR 0008). Option 5 is option 1 with a protocol in the
middle: the harness would be doing the work anyway.

Option 4 was rejected for the same reason as a backend-specific inference
client. Five endpoints are five endpoints; a client library would buy argument
validation that the port already performs, and would have to be mirrored.

### Consequences

- Good: the sandbox loses a capability instead of gaining one. The container
  still has no network, and now has one fewer reason to want it.
- Good: retrieval degrades to absence. The probe at startup checks both halves
  - the store answers, and the embedding model returns vectors of the width
  the collections were built at - and on failure the tool is simply not in the
  model's tool list. The run continues; `nih implement` says so in its header.
- Good: a width mismatch between the model and the collections is caught at
  startup by name and number, not by a wall of refused upserts mid-run.
- Bad: one tool now runs outside the container. It is read-only and cannot
  write, but the statement "every tool runs in the sandbox" is no longer true
  and the documentation has to say which one does not, and why.
- Bad: the harness process now holds an HTTP client to a second service for
  the length of a run.
- Neutral: a chunk id is not a Qdrant point id, so it is mapped to a UUIDv5 of
  itself and travels in the payload. Deterministic, so re-indexing an unchanged
  chunk overwrites its point instead of adding a second copy.
- That determinism is not enough on its own, and the port says so. A document
  chunk's id is derived from the document's content, so a *revised* document
  produces new ids and the previous version's chunks would stay indexed - and
  be cited as current, which for a corpus of datasheets and errata is the
  normal case rather than an edge one. `VectorStore.forget(collection, path)`
  exists for that: the path is the one identity that survives a revision, and
  `nih ingest` forgets it before writing. Verified by re-ingesting a revision
  that contradicted the version it replaced.
- The `vector = ["qdrant-client>=1.12"]` extra in `pyproject.toml` has been
  removed; it was written before this decision and installed a library nothing
  imports. The import-linter contracts still forbid `qdrant_client` in the
  domain, the ports and the application layer, which is what keeps this
  decision true rather than merely stated.

## Follow-ups

- Indexing the project's code (`chunk by function/class` with tree-sitter) and
  indexing compacted run context are separate pieces of work, tracked in
  `docs/roadmap.md`. Until the run collection exists, `rag_query` reports it as
  not indexed rather than failing.
