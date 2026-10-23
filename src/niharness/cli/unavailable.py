# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""Commands that need an adapter which is not written yet.

A command that cannot do its job says so and exits non-zero. It does not
pretend, and it does not half-run: a harness whose whole point is that red
means red cannot start by faking green.
"""

from __future__ import annotations

import typer

EX_UNAVAILABLE = 69
"""`sysexits.h`: the service is unavailable."""


def unavailable(command: str, needs: str, tracked_in: str) -> None:
    """Report that `command` is not wired up yet and exit.

    Args:
        command: the command the user asked for.
        needs: the adapter or subsystem that is missing.
        tracked_in: where the remaining work is written down.

    Raises:
        typer.Exit: always, with `EX_UNAVAILABLE`.
    """
    typer.secho(f"nih {command}: not available yet", fg=typer.colors.YELLOW, err=True)
    typer.echo(f"  needs: {_indented(needs, len('  needs: '))}", err=True)
    typer.echo(f"  tracked in: {tracked_in}", err=True)
    raise typer.Exit(EX_UNAVAILABLE)


def _indented(text: str, width: int) -> str:
    """Return `text` with its continuation lines aligned under the first.

    What is missing is often a sentence rather than a noun, and a sentence
    that wraps back to column zero reads as a second field.
    """
    lines = text.splitlines()
    return f"\n{' ' * width}".join(lines)
