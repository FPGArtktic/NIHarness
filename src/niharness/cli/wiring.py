# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""Wiring shared by the commands that run a model.

`nih plan` and `nih implement` are two stages of one pipeline, and they need
the same five things before either can start: a client for the served model,
a way to launch the tool server, a sandbox that is actually there, and two
lines of output saying whether the tools have a network and whether retrieval
is on. These lived in `cli/implement.py` until a second command needed them;
they are here so the two stages cannot disagree about what a run looks like.
"""

from __future__ import annotations

import sys
from pathlib import Path

import typer
from mcp import StdioServerParameters

from niharness.adapters.openai_compat import OpenAiCompatBackend
from niharness.adapters.podman import (
    PodmanSandbox,
    SandboxImageMissingError,
    SandboxUnavailableError,
)
from niharness.cli.retrieval import Retrieval
from niharness.config import HarnessConfig, ModelConfig
from niharness.ports.vcs import Worktree

EX_UNAVAILABLE = 69
"""`sysexits.h`: a service the command needs is not there."""


def server_parameters(
    config: HarnessConfig,
    worktree: Worktree,
    *,
    local: bool,
) -> StdioServerParameters:
    """Return how to start the tool server: in the sandbox, or on this host."""
    if local:
        return StdioServerParameters(
            command=sys.executable,
            args=["-m", "niharness.toolserver", "--root", worktree.path],
        )
    sandbox = PodmanSandbox(
        config.sandbox.image,
        Path(worktree.path),
        cpus=config.sandbox.cpus,
        memory=config.sandbox.memory,
        pids_limit=config.sandbox.pids_limit,
        binary=config.sandbox.binary,
        network=config.sandbox.network,
    )
    argv = sandbox.tool_server_argv()
    return StdioServerParameters(command=argv[0], args=list(argv[1:]))


async def check_sandbox(config: HarnessConfig, worktree: Worktree, *, command: str) -> None:
    """Fail early and clearly when Podman or the image is not there.

    Args:
        config: where the image and the Podman binary are named.
        worktree: the directory the container would mount.
        command: the command to name in the escape-hatch hint, so a planning
            run is not told how to restart an implementing one.

    Raises:
        typer.Exit: when the sandbox cannot be used.
    """
    sandbox = PodmanSandbox(
        config.sandbox.image,
        Path(worktree.path),
        binary=config.sandbox.binary,
        network=config.sandbox.network,
    )
    try:
        await sandbox.ensure_ready()
    except (SandboxUnavailableError, SandboxImageMissingError) as error:
        typer.secho(str(error), fg=typer.colors.RED, err=True)
        typer.echo(
            "  or run the tools on this host instead, outside the container:\n"
            f"    nih {command} --unsafe-local-tools",
            err=True,
        )
        raise typer.Exit(EX_UNAVAILABLE) from error


def network_note(config: HarnessConfig, local_tools: bool) -> str:
    """Say when the tools have a network, because the default is that they do not.

    Silence means `--network=none`. A run whose tools could reach the internet
    is a materially different run and the operator should not have to go and
    read `harness.toml` to find out which one this is.
    """
    if local_tools:
        return " (on this host, so whatever network this host has)"
    if config.sandbox.network == "none":
        return ""
    return f", network {config.sandbox.network}: its tools can reach the internet"


def retrieval_line(config: HarnessConfig, retrieval: Retrieval) -> str:
    """Say in one line whether `rag_query` will be offered, and over what."""
    if not retrieval.ready:
        return f"off, so rag_query is not offered ({retrieval.why})"
    where = ", ".join(
        (config.vector.code_collection, config.vector.doc_collection),
    )
    return f"{config.vector.url}, searching {where} and this run's own context"


def backend_for(model: ModelConfig) -> OpenAiCompatBackend:
    """Build a client for one served model."""
    return OpenAiCompatBackend(
        model.base_url,
        model.name,
        context_window=model.context_window,
        supports_grammar=model.supports_grammar,
        supports_reasoning=model.supports_reasoning,
        reasoning_effort=model.reasoning_effort,
        timeout_seconds=model.request_timeout_seconds,
    )
