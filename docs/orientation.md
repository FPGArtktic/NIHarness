<!--
SPDX-FileCopyrightText: 2026 Mateusz Okulanis
SPDX-License-Identifier: GPL-3.0-or-later
-->

# Orientation

Read this first if you are new to the repository - whether you are a person or
an agent working on it. It answers "what is this, what is already true, and what
must I not break", and it links to the page that goes deeper on each point.

## What NIHarness is

A harness that lets **local** models do real coding work under constraints that
make the output trustworthy:

1. Models propose; the harness decides. Every tool call is validated before it
   runs, every budget is enforced by the harness, every commit passes a gate.
2. Nothing leaves the machine. There is no network in any runtime code path.
3. Nothing is remembered implicitly. Run state is an event log; what matters
   between stages has to be written into an artifact.

It is not a chat wrapper, not an IDE plugin, and not a service. It is a
pipeline with gates, driven from a terminal, with a Rust TUI as a thin viewer.

## The shape of the code

| Layer | Package | Rule |
|---|---|---|
| Domain | `niharness.domain` | Pure. No I/O, no clock, no adapter imports. All the interesting decisions live here. |
| Ports | `niharness.ports` | Protocols only. Depends on the domain, nothing else. |
| Application | `niharness.application` | Drives the domain through the ports. No rules, no protocols, no adapter imports. |
| Management | `niharness.management` | Starts, stops and reports on runs, for whoever is driving. Never writes code, never commits. |
| Adapters | `niharness.adapters` | One adapter per port. Adapters never import each other. |
| CLI | `niharness.cli` | The only place that wires adapters into the domain. |

`uv run lint-imports` fails the build if that table is violated, so it is not a
suggestion. See [Architecture](concepts/architecture.md) and
[ADR 0002](adr/0002-hexagonal-architecture.md).

## What already works

- The whole domain core: artifact contracts, the event catalogue, tool-call
  validation with path confinement, role allowlists, budgets, the two-tier gate
  with failure signatures, loop detectors with the intervention ladder,
  compaction policy, and the fold that derives run state from events.
- The adapters: the JSONL event sink and control inbox, the OpenAI-compatible
  inference and embedding clients, the git worktree adapter, the rootless
  Podman sandbox, the MCP tool client, the PDF reader and the Qdrant vector
  store. Adapters never import each other; what they share - the air-gap check
  and HTTP failure reporting - is one module beside them.
- The step executor: it runs a step to completion against the ports, measures
  and compacts its window before every model call, and is tested with fakes
  rather than with a model and a container.
- The tool server (`niharness.toolserver`): the sandbox end of the protocol,
  holding the gate. The harness talks to it through the MCP adapter, and the two
  are tested end to end in one process.
- CLI commands that need no model: `nih init`, `nih config`, `nih gate`,
  `nih status`, `nih tools`, `nih plan check`, `nih plan import` (for
  conforming plans), and `nih docs generate`.
- `nih implement`: with a model served and the sandbox image built, it executes
  an approved plan task by task, each commit going through the gate.
- The control plane: `nih runs`, `nih cancel`, `nih resume`, `--detach`,
  `--json`, and `nih mcp` - the management surface an agent drives, which
  cannot touch the code. See
  [Managing from an agent](operations/managing-from-claude.md).
- Declared MCP servers: a project can name MCP servers of its own, import their
  tools and grant them to roles. They arrive namespaced, go through the same
  validation, and cannot commit. See
  [declared MCP servers](operations/mcp-servers.md).
- Language servers: `find_symbol`, `find_references` and `file_outline` ask a
  real language server inside the sandbox, by name, so navigation costs a line
  instead of a page. Verified against `clangd`. See
  [language servers](operations/language-servers.md).
- `replace_in_file`: an edit named by the text it replaces, with the harness
  computing the diff - so there are no hunk headers for a model to count
  wrong. This is what carried the project's first green commit, unattended, on
  a 4B. See [ADR 0022](adr/0022-an-edit-located-by-its-own-text.md).
- Documents and retrieval: `nih ingest` reads a PDF - its text layer where
  there is one, OCR where there is not - and indexes chunks that cite their
  pages; `rag_query` searches them, the code index and the run's own context,
  and answers with citations. It is the one tool that runs in the harness
  process rather than the sandbox, because the container has no network. See
  [documents](concepts/documents.md) and
  [retrieval](concepts/retrieval.md).

## What does not work yet

Commands that need a model, a container or a vector store exit with code 69 and
say what is missing. That is deliberate: see [the roadmap](roadmap.md) for the
order in which the gaps get filled.

Two of retrieval's three collections have no writer yet: the project's code is
not indexed (that needs tree-sitter chunking) and compaction does not yet write
what it drops. `rag_query` reports an unwritten collection as not indexed
rather than counting it as searched, so what works is honest about what does
not.

## Rules that hold everywhere

<div class="refusal" markdown>
These are the rules the code enforces. If a change needs one of them relaxed,
the change is wrong, or the rule needs an ADR that supersedes it - not a
workaround.
</div>

1. **Nothing red is committed.** The gate runs format, lint, build and quick
   tests; red output means no commit. [Gate](concepts/gate.md).
2. **No network at runtime.** A non-localhost backend URL is refused at
   construction. [ADR 0004](adr/0004-openai-compatible-inference-port.md).
3. **The domain stays pure.** Timestamps and sequence numbers are assigned at
   the edge, so replaying a log is deterministic.
4. **Everything is an event.** Never mutate run state; append to the log.
   [Events](concepts/events.md).
5. **Models never get a host shell.** `shell` is in no default allowlist, and
   tools run with `--network=none`. [Sandbox](operations/sandbox.md).
6. **Reasoning is logged, never fed back.** There is no field on a message that
   could carry it. [Prompts](concepts/prompts.md).
7. **Compaction never touches the event log.** It rewrites the model's window
   only. [Context](concepts/context.md).
8. **The TUI computes nothing.** Metrics, ETA and compaction decisions arrive as
   events.
9. **Token counts come from the backend tokenizer.** Never from a character
   heuristic.
10. **One logical change per commit**, Conventional Commits, body explains why.

## How to add something

| You are adding | You also add, in the same change |
|---|---|
| A subsystem | a port, an adapter, a contract test, a schema export if it adds an artifact, a docs page, an ADR |
| A step or role | the domain model, a prompt layer, a golden snapshot, an allowlist entry, a docs page, an ADR |
| A prompt change | the regenerated golden snapshot, and the observed behaviour change in the commit body |
| A compaction policy change | the hypothesis invariant first, then the policy |
| A dependency | an ADR note stating its license, and a wheelhouse rebuild |

## The commands you will actually run

```bash
uv sync --all-groups                 # install from the lockfile
uv run ruff format . && uv run ruff check .
uv run mypy --strict src
uv run pytest                        # unit + contract; integration is opt-in
uv run lint-imports                  # architecture boundaries
uv run reuse lint                    # SPDX headers
uv run nih docs generate             # regenerate schemas and reference pages
uv run mkdocs build --strict         # docs are part of the gate
```

Run the quick gate before proposing a commit. Do not rely on CI to tell you.
