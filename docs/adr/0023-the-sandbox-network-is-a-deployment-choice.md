<!--
SPDX-FileCopyrightText: 2026 Mateusz Okulanis
SPDX-License-Identifier: GPL-3.0-or-later
-->

# 0023. The sandbox's network is a deployment choice, not a constant

- Status: Accepted
- Date: 2026-10-23

## Context and problem statement

`--network=none` on the sandbox has been stated as non-negotiable since the
project began, and the rules said so in those words: no network at runtime, no
HTTP outside `localhost` in any code path.

That conflated two different things.

One is real and stays: **this harness has to run with no network at all.** The
target is an air-gapped network - defence, semiconductor, medical, anything
under export control - where the deliverable arrives as exported Podman images
and a wheelhouse, and nothing may dial out because there is nothing to dial.
Every design decision that follows from it holds: dependencies from the
lockfile, models served locally, no pulls, no downloads, documentation and
benchmarks that build offline.

The other was an assumption: that because the harness must *run* air-gapped, a
model's tools must never have a network **anywhere**. They are different
claims. A project developing on a connected network, exporting later, has
internet available and may reasonably want its tools to use it - and refusing
on its behalf buys that project nothing. The air gap is what the harness
exports *to*, not a property of every host it runs on.

## Decision drivers

- The air-gapped deployment must stay the default and must stay possible. A
  configuration that only works with a network would defeat the project.
- The gate is untouched. Network access changes nothing about what may become
  a commit.
- The harness's own traffic stays on loopback. That is a separate rule with a
  separate reason, and this decision does not relax it.
- A run that could reach the network is a different run, and six months later
  somebody will need to know which one it was.
- A hardening flag must not be settable by accident.

## Considered options

1. **A configured mode, `none` by default, recorded in the run.**
2. Keep `--network=none` constant and tell projects to work around it.
3. Always give the sandbox Podman's default networking.
4. `--network=host` when a network is wanted.

## Decision outcome

**Option 1.** `[sandbox] network` takes `none` or `private`.

`none` is the default and gives the container no interface at all, which is
what an air-gapped deployment requires. `private` maps to
`slirp4netns:allow_host_loopback=false`: the internet, and explicitly **not**
this host's loopback. That exclusion is the substance of the option rather
than a detail - the inference server, the vector store and the `nih mcp`
management surface all listen on loopback, and a tool that could dial them
would be outside the sandbox in the only sense that matters. Pinning the
parameter rather than relying on Podman's default is deliberate.

Anything else is refused at construction, with the two allowed values named.
`--network=hostt` is an error Podman would report; `--network=host` is not,
and it would share this host's entire namespace. Neither should be reachable
by misspelling a line of TOML, so the adapter validates and does not forward.

Everything else about the container is unchanged: `--read-only`,
`--cap-drop=ALL`, `--security-opt=no-new-privileges`, one writable bind mount,
the CPU, memory and pid caps.

The choice is recorded. `run.started` carries `sandbox_network`, and
`nih implement` prints it in its header when it is not `none`, because silence
should mean the strict default.

Option 2 is what the project did until now, and the reason to stop is that it
answers a question nobody asked on behalf of a project that can answer it
itself. Option 3 makes the air-gapped case the exception, which is backwards.
Option 4 was rejected: `host` is almost never what is wanted, and it is
exactly the mode that would expose the harness's own services to the tools it
is sandboxing.

### Consequences

- Good: a connected project can let its tools fetch what they need, and the
  same project exports to an air-gapped network without changing anything but
  one line, because `none` is what it was built to run with.
- Good: the strict mode is still enforced by the kernel rather than by
  convention, and is still the default - a project that never reads this ADR
  gets the air gap.
- Good: the run says which it was. A reviewer reading an event log six months
  later does not have to reconstruct the configuration.
- Bad: with `private`, a tool run can fetch and execute arbitrary code, and
  can send the worktree's contents anywhere. The gate still decides what
  becomes a commit, and that is all it decides - a tool that runs is a tool
  that ran. This is the trade a project makes knowingly, which is why it is
  opt-in, named in the run, and printed at startup.
- Bad: one more thing that differs between two hosts running the same plan.
  The recorded mode is what makes that diagnosable.
- Neutral: `add_dependency` still refuses anything absent from the local
  mirror. Whether it should consult a real index when the network is open is a
  separate decision with its own blast radius, and is not taken here.

## How this stands to ADR 0021

[ADR 0021](0021-retrieval-runs-in-the-harness.md) put `rag_query` in the
harness process because "the sandbox is started `--network=none` and the
vector store is a service on the host", and listed that flag among its
drivers. It is not superseded, and the reason is the choice of what `private`
means: it reaches the internet and **not** this host's loopback, which is where
the store listens. So the sandbox cannot reach the store under either mode,
and 0021's conclusion holds without an exception rather than with one. Had
`host` been the option offered, that would not have been true - which is part
of why it was not.

## What does not change

The harness's own HTTP stays on loopback, enforced by `require_local` in
`adapters/local_http.py` and by the import-linter contracts: inference,
embeddings and the vector store are local services, a non-loopback URL is
refused at construction, and there is no flag for it. That rule exists because
the *harness* is what gets exported, and it is unaffected by what the sandbox
is allowed to reach.
