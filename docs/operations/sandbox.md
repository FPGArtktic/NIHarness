<!--
SPDX-FileCopyrightText: 2026 Mateusz Okulanis
SPDX-License-Identifier: GPL-3.0-or-later
-->

# Sandbox

Models never get a host shell. Every tool call runs inside a rootless Podman
container with the project directory bind-mounted and no network interface at
all.

```bash
podman run --rm \
  --network=none \
  --read-only --read-only-tmpfs \
  --volume "$PWD/.nih/worktrees/$RUN:/work:rw,Z" \
  --workdir /work \
  --userns=keep-id \
  --cap-drop=ALL --security-opt=no-new-privileges \
  --cpus 8 --memory 8g --pids-limit 512 \
  --timeout 120 \
  localhost/niharness-sandbox:latest "$@"
```

That is not an illustration: it is what `PodmanSandbox.container_argv` builds,
flag for flag. The method is a pure function of the configuration, so a
contract test asserts every flag above on a machine that has no Podman at all,
and the integration tests then confirm the properties only a real container can
show - the worktree is writable, the root filesystem is not, `/proc/net/route`
is empty, a red command stays red, a hanging command is stopped.

| Setting | Why |
|---|---|
| `--network=none` | the air gap, enforced at the kernel rather than by convention |
| only the run worktree is writable | a tool cannot touch the user's checkout, let alone the host |
| `--cap-drop=ALL`, `no-new-privileges` | a compiler or test runner has no business with capabilities |
| cpu, memory and pid caps | a runaway test suite or a fork bomb in a build script must not take the machine down |
| `--userns=keep-id` | files the agent writes stay owned by you, not by a subuid |
| `--timeout`, plus a longer process-level backstop | a hanging command is stopped, and so is a hanging Podman |
| rootless | a container escape lands as an unprivileged user |

One container per command, never a long-lived one: nothing leaks between
commands, and the trade-off is written down in
[ADR 0008](../adr/0008-sandbox-is-one-container-per-command.md).

## Defence in depth for paths

Path confinement is checked twice. The domain rejects absolute paths, `~` and
any lexical escape before a call is dispatched; the sandbox then cannot reach
outside the bind-mount even if the lexical check were wrong. Symlink resolution
belongs to the sandbox, because only it can see a filesystem.

## Tool results

Results are truncated to a byte cap, keeping the head **and** the tail - build
errors live at the end of the output - and the elision is marked. The full
result never enters the window; its digest does, which is also what the
progress-stall detector compares.

## The tool server

The harness is an MCP client; the server is this project's own package, started
inside the image and bound to the worktree:

```bash
podman run -i --network=none --read-only --read-only-tmpfs \
  --volume "$PWD/.nih/worktrees/$RUN:/work:rw,Z" --workdir /work \
  --userns=keep-id --cap-drop=ALL --security-opt=no-new-privileges \
  localhost/niharness-sandbox:latest \
  python -m niharness.toolserver --root /work
```

It offers exactly the tools in `niharness.domain.tools` - `read_file`,
`list_tree`, `grep`, `apply_patch`, `run_lint`, `run_build`, `run_tests`,
`git_commit` - with the same argument names, because the harness validates a
call against that schema before dispatching it. A server that renamed an
argument would simply never be called.

Three things are worth knowing about it:

- **It confines paths again.** The domain rejects absolute paths and lexical
  escapes before dispatch; the server resolves every path and refuses anything
  that lands outside the root, which is what catches a symlink. The container is
  the third layer, and the one that holds if the first two are ever wrong.
- **It holds the gate.** `git_commit` runs format, lint, build and the quick
  tests, and commits only on green. A red gate comes back as a refusal carrying
  the failing command's output - the thing the implementer needs to fix the
  cause. There is no flag that skips it.
- **It answers with an outcome envelope** (`{"ok", "text"}`), because MCP's own
  error channel masks the detail and the detail is the point. See
  [ADR 0013](../adr/0013-tools-answer-with-an-outcome-envelope.md).

This is the one long-lived container: a stdio session has state, unlike the
per-command containers the gate's own commands run in.

## Images

| Image | Contents |
|---|---|
| `niharness-sandbox` | the layered language toolchains the gate drives |
| `niharness-yocto` | a Debian-based image with persistent `DL_DIR` and `SSTATE_DIR`, `BB_NO_NETWORK=1` |

Images are built once on a networked machine and loaded with `podman load` on
the air-gapped one. The harness never pulls.

Run the integration tests against an image you already have:

```bash
NIH_SANDBOX_IMAGE=localhost/niharness-sandbox:latest uv run pytest -m integration
```

!!! warning "Still missing"
    The `Sandbox` adapter is in place; the sandbox **image** has not been built
    in this repository yet, and the MCP tool servers that will run through this
    port are the next step on [the roadmap](../roadmap.md). `nih init` does not
    render the Quadlet units yet either.
