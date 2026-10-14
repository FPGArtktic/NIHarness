<!--
SPDX-FileCopyrightText: 2026 Mateusz Okulanis
SPDX-License-Identifier: GPL-3.0-or-later
-->

# Install

## Requirements

| Thing | Why | Note |
|---|---|---|
| Python 3.12+ | the harness core | `uv` can manage the interpreter |
| `uv` | dependency management | the only package manager used; never call `pip` directly |
| git 2.40+ | per-run worktrees | the user's checkout is never modified |
| Podman (rootless) | the tool sandbox | `--network=none` is not optional |
| Rust 1.80+ | the TUI crate | only needed to build `tui/` |
| `llama-server` | the main model | OpenAI-compatible HTTP on localhost |
| `bitnet.cpp` | the utility model | summaries, compaction, routing |
| Qdrant | retrieval | optional until the retrieval stage lands |

## Development install

```bash
git clone https://github.com/FPGArtktic/NIHarness
cd NIHarness
uv sync --all-groups        # from the lockfile, with hashes
uv run nih version
```

## Air-gapped install

The harness never reaches the network at runtime, and installation should not
either. On a machine that *does* have network, build a wheelhouse:

```bash
uv export --frozen --all-groups --no-emit-project -o requirements.lock
uv pip download -r requirements.lock -d wheelhouse/
```

Carry `wheelhouse/` and the repository across, then:

```bash
uv sync --frozen --offline --all-groups
```

!!! note "Models are not distributed with the repository"
    Download the GGUF weights separately and record their licenses - see
    [Models](../operations/models.md). Nothing in the harness will fetch a
    model for you, by design.

## Verifying the install

```bash
uv run ruff check . && uv run mypy --strict src
uv run pytest                 # unit + contract; integration needs Podman
uv run lint-imports           # architecture boundaries
uv run reuse lint             # SPDX headers
uv run nih gate --tier quick  # what the gate would run here
```

If `nih gate` prints a command you do not have installed, that language's
toolchain is missing; the gate reports it as red rather than skipping it.
