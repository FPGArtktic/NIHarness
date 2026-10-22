# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""Printing, in the two shapes a command can be asked for.

Every command that prints something a machine might want prints it the same
way: a human form by default, one JSON object with `--json`. The JSON is the
substrate the management surface is built on, so it is not a debugging
convenience - it is the contract.
"""

from __future__ import annotations

import json
from typing import Any

import typer
from pydantic import BaseModel


def dumps(payload: Any) -> str:  # noqa: ANN401 - any JSON-serialisable payload
    """Serialise `payload` deterministically."""
    if isinstance(payload, BaseModel):
        return payload.model_dump_json(indent=2)
    return json.dumps(payload, indent=2, sort_keys=True, default=str)


def emit(payload: Any, *, json_mode: bool, human: str = "") -> None:  # noqa: ANN401
    """Print one result.

    Args:
        payload: the machine-readable object.
        json_mode: print that object instead of the human form.
        human: what to print for a person; the JSON form when empty.
    """
    typer.echo(dumps(payload) if json_mode or not human else human)


def fail(message: str, payload: Any = None, *, json_mode: bool, code: int = 1) -> None:  # noqa: ANN401
    """Report a failure in the requested shape and exit.

    Raises:
        typer.Exit: always, with `code`.
    """
    if json_mode:
        typer.echo(dumps({"ok": False, "error": message, **(payload or {})}))
    else:
        typer.secho(message, fg=typer.colors.RED, err=True)
    raise typer.Exit(code)
