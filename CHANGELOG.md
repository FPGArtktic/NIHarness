<!--
SPDX-FileCopyrightText: 2026 Mateusz Okulanis
SPDX-License-Identifier: GPL-3.0-or-later
-->

# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Logging resolves `sys.stderr` at write time, so a captured or replaced stream
  does not make every later record raise on a closed file.

- Repository bootstrap: PEP 621 packaging with `uv`, GPL-3.0-or-later licensing,
  REUSE compliance, ruff/mypy/pytest/import-linter configuration.
- Domain core: artifact contracts (`Spec`, `Plan`, `Questions`, `Verdict`,
  `ContextSummary`, `FinalAnswer`), the event catalogue, tool contracts with call
  validation and path confinement, role definitions with per-role allowlists,
  budgets with forced finalisation, the two-tier quality gate with failure
  signatures, tool-loop detectors with the intervention ladder, compaction policy,
  and the event fold that derives run state.
- Tool loop as a pure state machine: it decides the next action and folds what
  comes back into state plus the events to append, so a bounded ReAct loop is
  unit-testable without a model or a container.
- Control plane: commands (`CancelRun`, `ApprovePlan`, `AnswerQuestions`) arrive
  through an append-only inbox beside the event log and become events when the
  run acts on them, so a cancel from the TUI, from `nih cancel` and from an
  agent are one mechanism.
- Management surface: `niharness.management` with the run registry, plan store
  and bounded views; `nih runs`, `nih cancel`, `nih implement --detach`, `--json`
  on the commands that print, and `nih mcp` - an MCP server an agent drives,
  with no path to reading or writing project code.
- The TUI follows a live run (`nih-tui --run <dir>`) and can ask it to stop.
- `nih resume <run-id>` and `run_resume`: a run now records what it concluded
  about each task, so a cancelled run continues without redoing committed work,
  and refuses when it cannot know what it was running.
- The first live model run, against a real `llama-server`, found four things
  that fakes could not. Each is fixed with a test that fails without the fix:
  the artifact's schema was demanded on turns that also offered tools, which is
  a contradiction a model cannot satisfy and a backend refuses outright;
  `apply_patch` rejected diffs over clerical mistakes a model makes constantly
  (missing `a/`/`b/` prefixes, hunk counts counted by eye, a new file announced
  without `--- /dev/null`) and reported git's "corrupt patch at line 8" instead
  of the line and the rule; a backend's refusal reached the operator as "400
  Bad Request" with the server's explanation thrown away; and a reasoning model
  spends the output budget on thinking, with no way to bound it - `[models.*]
  reasoning_effort` now sets the standard field.
- Declared MCP servers: `[[mcp.servers]]` in `harness.toml` names servers the
  harness did not write, the tools to import from each and the roles granted
  them. Tools arrive as `<server>__<tool>`, so nothing a server advertises can
  be mistaken for a built-in one; arguments are checked against the schema the
  server advertised; a declared server that cannot be started, or that does not
  offer an imported tool, stops the run before it begins. `nih tools` prints
  the whole surface per role, starting the declared servers to ask them. A run
  records a `tools.attached` event for each, and none of them can commit.
- Language servers in the sandbox: `find_symbol`, `find_references` and
  `file_outline`, addressed by name rather than by line and column, so a model
  never has to read a file in order to ask about it. One resident server per
  language, started on demand; the table of which server serves which language
  is domain data beside the gate matrix, overridable under `[lsp]`. When a
  server's index is cold - which is what a real clangd does - the files that
  mention the name are asked directly, so an answer does not depend on a
  background index. `apply_patch` tells the servers what it changed.
- Tool server (`niharness.toolserver`): an MCP server for one worktree, offering
  the harness's own tools, resolving every path against the root, and holding the
  gate - `git_commit` runs the quick tier and refuses with the failing output
  rather than committing on red. Tools answer with an `{ok, text}` envelope,
  because MCP's error channel masks the detail the model needs.
- Application layer with `StepRunner`: runs one step to completion against the
  ports, performing what the loop decides and appending the events it returns.
- `Window`: measures the model's window through the `Tokenizer` port, applies the
  compaction policy before every model call, emits `context.compacted`, and keeps
  the message list in the order the protocol requires.
- `ModelSummarizer`: produces a `ContextSummary` with the utility model when the
  deterministic compaction passes are not enough, and drops any file it claims
  to have seen that the summarised text never mentioned.
- Ports: `ModelBackend`, `Tokenizer`, `ToolProvider`, `VectorStore`, `Vcs`,
  `Sandbox`, `EventSink`.
- Adapters: JSONL event sink, OpenAI-compatible inference client, git worktree
  adapter, the rootless Podman sandbox (one hardened container per command, with
  the argument vector asserted by a contract test), and the MCP tool client
  (only known and allowed tools are forwarded to the model).
- CLI (`nih`) with `init`, `config`, `gate`, `plan check`, `plan import`,
  `status`, `docs generate`, and `implement` - which runs an approved plan in a
  dedicated worktree with the tool server in the sandbox, taking each task
  through implement, the commit gate, review, and bounded fixes.
- Prompt layers for every role, plus the Definition of Done checklist.
- Documentation site (MkDocs Material) with concept pages, a generated event
  catalogue and the first architecture decision records.
- Rust TUI crate skeleton (ratatui) that renders the run event stream.
