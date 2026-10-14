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
  --cpus 8 --memory 8g \
  --cap-drop=ALL --security-opt no-new-privileges \
  localhost/niharness-sandbox:latest "$@"
```

| Setting | Why |
|---|---|
| `--network=none` | the air gap, enforced at the kernel rather than by convention |
| only the run worktree is writable | a tool cannot touch the user's checkout, let alone the host |
| `--cap-drop=ALL`, `no-new-privileges` | a compiler or test runner has no business with capabilities |
| cpu and memory caps | a runaway test suite must not take the machine down |
| rootless | a container escape lands as an unprivileged user |

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

## Images

| Image | Contents |
|---|---|
| `niharness-sandbox` | the layered language toolchains the gate drives |
| `niharness-yocto` | a Debian-based image with persistent `DL_DIR` and `SSTATE_DIR`, `BB_NO_NETWORK=1` |

Images are built once on a networked machine and loaded with `podman load` on
the air-gapped one. The harness never pulls.

!!! warning "Not written yet"
    `containers/` holds the plan for these images; the `Sandbox` adapter and the
    Quadlet units are the next stage on [the roadmap](../roadmap.md).
