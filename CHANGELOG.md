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
- Ports: `ModelBackend`, `Tokenizer`, `ToolProvider`, `VectorStore`, `Vcs`,
  `Sandbox`, `EventSink`.
- Adapters: JSONL event sink, OpenAI-compatible inference client, git worktree
  adapter, and the rootless Podman sandbox (one hardened container per command,
  with the argument vector asserted by a contract test).
- CLI skeleton (`nih`) with `init`, `plan`, `implement`, `status`, `docs generate`.
- Prompt layers for every role, plus the Definition of Done checklist.
- Documentation site (MkDocs Material) with concept pages, a generated event
  catalogue and the first architecture decision records.
- Rust TUI crate skeleton (ratatui) that renders the run event stream.
