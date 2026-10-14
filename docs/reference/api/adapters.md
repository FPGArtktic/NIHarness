<!--
SPDX-FileCopyrightText: 2026 Mateusz Okulanis
SPDX-License-Identifier: GPL-3.0-or-later
-->

# API: adapters

Implementations of the ports. Each is covered by a contract test that runs
against recorded responses, never a live backend.

## Event sink (JSONL)

::: niharness.adapters.jsonl_events.sink

## Inference (OpenAI-compatible HTTP)

::: niharness.adapters.openai_compat.client

## Tools (MCP)

::: niharness.adapters.mcp.provider

## Sandbox (rootless Podman)

::: niharness.adapters.podman.sandbox

## Version control (git worktrees)

::: niharness.adapters.git.vcs

## Configuration

::: niharness.config

## Prompt layers

::: niharness.prompts
