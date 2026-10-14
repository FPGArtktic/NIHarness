<!--
SPDX-FileCopyrightText: 2026 Mateusz Okulanis
SPDX-License-Identifier: GPL-3.0-or-later
-->

# The gate

`git_commit` is not a convenience wrapper around `git`. It is the gate, and the
only way a change becomes a commit.

<div class="refusal" markdown>
Red output means there is no commit. This applies to agents and to you. There is
no flag, no environment variable and no "just this once".
</div>

## Two tiers

| Tier | When | What runs |
|---|---|---|
| quick | every commit attempt | format, lint, incremental build, unit tests, and the documentation build when documentation changed |
| full | before a merge, and at the end of a run | complete build, integration tests, sanitizers, bitbake, ptest, documentation link check |

Yocto never runs in the quick tier: a recipe parse plus a bitbake run would make
every commit attempt cost minutes.

## Language detection

Languages are detected by **marker file**, never by counting extensions, so a
stray `main.go` in a Python repository does not drag in the Go toolchain.

=== "Python"

    Marker: `pyproject.toml`, `setup.cfg`

    ```text
    ruff format --check .     (format)
    ruff check .              (lint)
    mypy --strict src         (lint)
    lint-imports              (lint)
    reuse lint                (lint)
    pytest                    (test)
    pytest -m integration     (test, full tier only)
    ```

=== "Rust"

    Marker: `Cargo.toml`

    ```text
    cargo fmt --check                 (format)
    cargo clippy -- -D warnings       (lint)
    cargo build                       (build)
    cargo test                        (test)
    cargo deny check licenses         (lint, full tier only)
    ```

=== "C/C++"

    Marker: `CMakeLists.txt`, `Makefile`, `meson.build`

    ```text
    clang-format --dry-run --Werror   (format)
    clang-tidy -p build               (lint)
    cppcheck --error-exitcode=1 .     (lint)
    cmake --build build               (build)
    ctest --output-on-failure         (test)
    ctest in build-asan               (test, full tier only)
    ```

The complete matrix, including Go, Bash, TCL, HDL, Yocto and documentation, is
in the generated [configuration reference](../reference/config.md).

## Ordering

Commands run cheapest and most specific first: format, lint, build, test, docs.
A formatting failure must not wait for a test suite. Within a kind, commands are
sorted by language and name, so a gate plan is deterministic and diffable.

## Failure signatures

A failure is normalised before it is compared: absolute paths, line numbers,
durations and addresses are replaced with placeholders, and only diagnostic
lines are kept. The digest of that normalised text is the **failure signature**.

```text
/home/a/repo/tests/test_x.py:41: error: bad thing in 9.1s
/tmp/other/tests/test_x.py:77: error: bad thing in 12.4s
                      ↓ both normalise to
error: bad thing in <dur>          →  signature 8f3c1d...
```

Two runs that fail the same way share a signature. That is what makes
[gate thrash](tool-loops.md) detectable: the same signature three times means
the model is not making progress, whatever it says in prose.

## Rollback

On red, the worktree is reset to the last green commit and the step restarts
with a summary of what failed, so the same wrong patch is not re-applied.
