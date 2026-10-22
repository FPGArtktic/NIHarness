<!--
SPDX-FileCopyrightText: 2026 Mateusz Okulanis
SPDX-License-Identifier: GPL-3.0-or-later
-->

# Language servers

Three of the tools in the sandbox do not read files - they ask a language
server:

| Tool | Question | Instead of |
|---|---|---|
| `find_symbol(name)` | where is this defined, and what is its signature? | `grep` then `read_file` |
| `find_references(name)` | who uses this? | `grep`, including comments and strings |
| `file_outline(path)` | what is in this file? | `read_file` on the whole file |

All three are read-only, so every role has them, and all three are addressed
**by name**. A tool that asked for a line and a column would make the model
read the file first, which is the cost these tools exist to remove.

```text
find_symbol("checksum")
  src/net.c:3  function checksum
      int checksum(int a, int b)

find_references("checksum")
  checksum is defined at src/net.c:3 and used:
    src/net.c:10  return checksum(1, 2) + checksum(3, 4);
```

That is a real answer from a real `clangd`. The `grep` for the same question
returns the definition, both call sites, the declaration in the header, and
anything that happens to contain the string.

## Which server serves which language

The table is domain data - `niharness.domain.language_servers` - beside the
gate matrix and over the same languages. Each language names *candidates* in
preference order, because the same language is served by several
implementations and an image ships whichever one it has.

| Language | Candidates, in order |
|---|---|
| C / C++ | `clangd`, `ccls` |
| Python | `basedpyright-langserver`, `pyright-langserver`, `jedi-language-server`, `pylsp` |
| Rust | `rust-analyzer` |
| Go | `gopls` |
| Bash | `bash-language-server` |
| HDL | `verible-verilog-ls`, `svls`, `vhdl_ls` |
| Yocto | `bitbake-language-server` |
| Docs | `marksman` |
| TCL | *none* - deliberately. `grep` still works, and the gate still lints it. |

The first candidate found on the machine that runs the tools wins. Nothing is
guessed: a language with no server installed is reported as unserved, and the
tools say so instead of answering emptily.

## Checking what you have

```bash
uv run nih tools
```

```text
language servers, for find_symbol/find_references/file_outline:
  docs          NOT INSTALLED on this host
  python        NOT INSTALLED on this host
  rust          NOT INSTALLED on this host
  (a sandboxed run uses the image's servers, not this host's)
```

That last line is the one to remember. `nih tools` inspects the host, which is
what matters for `--unsafe-local-tools`; a sandboxed run uses the servers in
the image.

## Configuring them

```toml
# harness.toml
[lsp]
enabled = true                      # (1)!
disabled_languages = ["docs"]       # (2)!
start_timeout_seconds = 30.0        # (3)!
request_timeout_seconds = 20.0

[lsp.servers.python]                # (4)!
command = "pylsp"
```

1. `false` turns the three tools off. They then refuse with "language servers
   are not enabled for this run; use grep instead" - a refusal that names the
   alternative, rather than an empty answer.
2. A language you do not want served, even if its server is installed.
3. The handshake budget. A cold index is paid for here, once per language, and
   the table raises it where a language needs more: rust-analyzer 60s, clangd
   45s.
4. A command of your own, winning over the shipped candidates.

## What the harness does about a cold index

A language server builds its index in the background, and a cold index answers
*nothing* - the same answer as a name that does not exist. Measured against a
real clangd on a one-file project, the index was still empty when the first
question arrived.

So there are two routes to a definition:

1. `workspace/symbol`, the server's index. Fast and complete when it is warm.
2. When that answers nothing: the files that mention the name are found with a
   plain scan and handed to the server one at a time, and the server is asked
   what each one defines. The scan only chooses *where to ask* - the answer is
   still the server's, so a name in a comment is still not a definition.

The retry on an empty index is about a second, deliberately. Waiting out a
whole warm-up on every miss would charge a model's typo the price of a cold
start, which is minutes over a run.

## Keeping the server honest

`apply_patch` is the only thing in the harness that changes the worktree, which
makes it the one place a server's view can be corrected. After a patch applies,
open documents get a `textDocument/didChange` with their new text and the rest
of the change is announced with `workspace/didChangeWatchedFiles`. A reviewer
asking about a symbol after a fix sees the fixed code.

## Limits worth knowing

- **One resident server per language per run.** A deliberate exception to
  [ADR 0008](../adr/0008-sandbox-is-one-container-per-command.md): a gate
  command gets a fresh container because a build must not inherit state, while
  an index is worth keeping.
- **Cross-file references in C/C++ need clangd's index.** Within a translation
  unit they are exact immediately; across one, the background index has to
  catch up.
- **The image grows.** clangd alone is a few hundred megabytes, which is why
  the language-server layer in `containers/Containerfile.sandbox` is separate
  and can be dropped.
- **No diagnostics tool yet.** The client collects `publishDiagnostics`, but
  nothing exposes them; the gate is what says whether the code is good. See
  [the roadmap](../roadmap.md).

The decision, the alternatives and what measuring a real server changed are in
[ADR 0020](../adr/0020-language-servers-in-the-sandbox.md).
