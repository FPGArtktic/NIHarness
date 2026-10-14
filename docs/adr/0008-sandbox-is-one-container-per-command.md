<!--
SPDX-FileCopyrightText: 2026 Mateusz Okulanis
SPDX-License-Identifier: GPL-3.0-or-later
-->

# 0008. One hardened container per command

- Status: Accepted
- Date: 2026-10-14

## Context and problem statement

Models never get a host shell, so every command the harness runs on a model's
behalf needs isolation: tool calls like `grep` and `read_file`, and gate
commands like `cargo build` and `pytest`. That isolation runs on every commit
attempt, dozens of times per task, so it has to be both strict and cheap. It
also has to work on a machine with no network, where there is nothing to pull.

## Decision drivers

- The air gap should be enforced by the kernel, not by code review.
- A tool must not be able to write anywhere except the run's worktree.
- The hardening must be verifiable without a container, or it will not be
  tested, and an untested security property is a hope.
- Rootless: an escape should land as an unprivileged user.
- Startup cost must be small enough to pay per command.

## Considered options

1. **One short-lived rootless container per command** (`podman run --rm`).
2. One long-lived container per run, with `podman exec` per command.
3. Run on the host, relying on the domain's path confinement alone.
4. A microVM per run.

## Decision outcome

Option 1. `PodmanSandbox.container_argv` builds the command, and it is a pure
function of the configuration, which is what lets a contract test assert the
flags on a machine without Podman:

| Flag | What it buys |
|---|---|
| `--network=none` | no network interface at all; the air gap, in the kernel |
| `--read-only`, `--read-only-tmpfs` | the only writable path is the bind-mount |
| `--volume <worktree>:/work:rw,Z` | exactly one writable path, correctly labelled for SELinux |
| `--workdir /work/<confined cwd>` | the working directory is confined by the domain first |
| `--userns=keep-id` | files the agent writes stay owned by the user, not by a subuid |
| `--cap-drop=ALL`, `--security-opt=no-new-privileges` | a compiler has no business with capabilities |
| `--cpus`, `--memory`, `--pids-limit` | a runaway test suite or fork bomb cannot take the machine |
| `--timeout` plus a longer process-level backstop | a hanging command is stopped, and a hanging Podman is too |

Option 3 was rejected: lexical path confinement is a good first check, which is
why the domain does it, but it is not a boundary - a build script can write
anywhere its user can. Option 4 costs far more startup than the threat here
justifies, given rootless containers and no network. Option 2 remains the
obvious optimisation if container startup shows up as harness overhead in the
L3 benchmark, which reports that separately from inference time for exactly
this kind of question; it is not taken now because per-command containers keep
state from leaking between commands for free.

## Consequences

- Each command pays container startup. That cost is measured, not guessed: L3
  records harness overhead apart from inference.
- Nothing survives between commands unless it is an explicit volume. Caches
  that must persist (`ccache`, Yocto's `DL_DIR` and `SSTATE_DIR`) are therefore
  explicit mounts in the image that needs them, which is also what makes them
  reviewable.
- The harness never pulls an image. A missing image is an error that prints the
  `podman build` and `podman save`/`podman load` commands, because on the
  target host there is no registry to reach.
- The host environment is not inherited; only variables the caller passes
  reach the command, and their names must be plain uppercase identifiers.
- Podman is not a Python dependency: it is invoked as a binary, and the
  injected runner is what keeps the contract tests hermetic.
- MCP tool servers, when they arrive, run through this same port rather than
  beside it.
