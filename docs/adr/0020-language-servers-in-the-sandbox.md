<!--
SPDX-FileCopyrightText: 2026 Mateusz Okulanis
SPDX-License-Identifier: GPL-3.0-or-later
-->

# 0020. Language servers run in the sandbox, and are asked by name

- Status: Accepted
- Date: 2026-10-22

## Context and problem statement

Until now a model navigating a codebase had `read_file`, `list_tree` and
`grep`. That is the toolkit of someone reading a project over a telephone line.
Asking "where is `validate_call` defined?" costs a `grep` whose answer is forty
lines, most of them call sites, comments and the word inside a string, followed
by a `read_file` to find out which of them was the definition.

The project's premise is token economics: the local 30B model does the work and
every token it spends is wall-clock on a CPU. Navigation was the largest
avoidable part of that spend, and it was also the least *accurate* part - a
string match does not know a definition from a comment.

Language servers answer exactly these questions, every language in the gate
matrix has one, and they are already how editors answer them. The protocol is
not MCP, so the mechanism from [ADR 0019](0019-declared-mcp-servers.md) does
not apply: LSP is JSON-RPC with a lifecycle of its own.

## Decision drivers

- Tokens: a definition should cost a line, not a page.
- A weak model cannot be asked for a line and a column. A tool whose arguments
  require the model to read the file first has saved nothing.
- The air gap: LSP is stdio, so this adds no network path. The servers have to
  live where the tools live, which is the sandbox - a sandboxed run cannot see
  the host's toolchain.
- The gate is untouched. These tools read.
- Everything stays an event, path-confined, truncated and budgeted, like every
  other tool.

## Considered options

1. **An LSP client inside the tool server, exposing tools addressed by name.**
2. An LSP-to-MCP bridge, declared as an external server (ADR 0019).
3. The harness's own index: tree-sitter plus the vector store already on the
   roadmap.
4. Tools that mirror LSP: `definition(path, line, column)`, and so on.

## Decision outcome

Option 1, with three tools: `find_symbol`, `find_references` and
`file_outline`. All read-only, so every role has them.

**Addressed by name.** `workspace/symbol` turns a name into a position, and the
position is what `textDocument/references` and `textDocument/hover` need. This
is the whole reason option 4 was rejected: a tool that takes a line and a
column forces the model to read the file to count them, which is the cost the
tool exists to remove.

**In the sandbox, resident.** The servers are installed in the sandbox image
and started lazily, one per language, living as long as the tool server does.
This is a deliberate exception to
[ADR 0008](0008-sandbox-is-one-container-per-command.md): a gate command gets a
fresh container every time because a build must not inherit state, while an
index is worth keeping and costs tens of seconds to rebuild.

**Which server serves what is domain data.** `niharness.domain.language_servers`
is a table beside the gate matrix, over the same `Language` enum: candidates in
preference order, the suffixes each owns, and how long its handshake may take.
The domain holds the policy and starts nothing; `harness.toml` overrides a
command under `[lsp]`.

**Synchronisation has exactly one writer.** `apply_patch` is the only thing in
the harness that changes the worktree, so it is the one place a server's view
can be kept honest: open documents get a `didChange` with the new text and the
rest is announced with `workspace/didChangeWatchedFiles`.

Option 2 would have cost no harness code, but a bridge outside the sandbox
cannot be path-confined and is another process to trust and maintain. Option 3
is still on the roadmap and answers a different question - *what is this like*,
fuzzily, across history - where a language server answers *what is this*,
exactly, right now. They are complements, not alternatives.

## Consequences

- `ToolName` grows three members and `READ_ONLY_TOOLS` grows with it, so every
  role including the reviewer and the brainstormer can navigate precisely.
- The sandbox image grows. clangd alone is a few hundred megabytes, so the
  language-server layer is separate and `[lsp] enabled = false` turns the tools
  off; they then say so rather than failing.
- **Measured against a real clangd**, two things were wrong in the first
  design, and both are now part of it:
    - `workspace/symbol` answered *nothing* on a one-file project, because the
      background index was not built yet. An index is not a prerequisite the
      harness can rely on, so when it answers nothing the files that mention
      the name are found with a plain scan and handed to the server one at a
      time. The scan chooses where to ask; the server still decides the answer,
      so a name in a comment is still not a definition.
    - waiting out a warm-up on every miss charged a model's typo the price of a
      cold start. The retry is bounded to about a second; a cold start is paid
      for once, in the handshake, where a per-language budget allows it
      (rust-analyzer 60s, clangd 45s).
- Servers disagree about hover: pyright returns a fenced signature, clangd a
  markdown document with a heading and a parameter list. The line with
  parentheses is preferred, which is the signature in both.
- Most servers answer `textDocument/*` only about documents they were given, so
  a document is opened before it is asked about. Without that the answer is an
  empty list and the reason is invisible.
- TCL is in the gate matrix and has no server in the table. It is listed as
  deliberately unserved, and a test fails if a language falls off both lists.
- A client that never answers the server's own requests
  (`workspace/configuration`, `client/registerCapability`) hangs a real pyright.
  The client answers them, and the fake server in the contract tests asks one
  so that this cannot regress unnoticed.
