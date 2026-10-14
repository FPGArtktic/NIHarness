# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""Versioned prompt layers.

A prompt is assembled from layers: the immutable policy, the role layer, the
task, retrieved context and, for roles that must finish something, the
Definition of Done checklist. Layers are package data, so a wheel carries the
exact prompts a run used, and a prompt change is a commit with a golden-test
update.
"""

from __future__ import annotations

from functools import cache
from importlib import resources

PACKAGE = "niharness.prompts"


@cache
def load_layer(name: str) -> str:
    """Return the text of one prompt layer.

    Args:
        name: the file name of the layer, for example `00-policy.md`.

    Returns:
        The layer text with the SPDX comment header removed, so licensing
        metadata never reaches the model's context.

    Raises:
        FileNotFoundError: if the layer is not part of the package.
    """
    text = resources.files(PACKAGE).joinpath(name).read_text(encoding="utf-8")
    return strip_license_header(text).strip()


def load_layers(names: tuple[str, ...]) -> tuple[str, ...]:
    """Return the texts of `names`, in order."""
    return tuple(load_layer(name) for name in names)


def strip_license_header(text: str) -> str:
    """Remove a leading HTML comment block from a Markdown layer."""
    stripped = text.lstrip()
    if not stripped.startswith("<!--"):
        return text
    _, _, rest = stripped.partition("-->")
    return rest.lstrip("\n")


def available_layers() -> tuple[str, ...]:
    """Return every shipped layer file name, sorted."""
    return tuple(
        sorted(
            entry.name
            for entry in resources.files(PACKAGE).iterdir()
            if entry.name.endswith(".md")
        ),
    )


SEPARATOR = "\n\n---\n\n"
"""What joins layers into one system prompt."""


def assemble(names: tuple[str, ...]) -> str:
    """Return the layers of `names` joined into one prompt.

    Args:
        names: layer file names, in stack order.

    Returns:
        The assembled prompt. Golden tests pin this output, so a prompt change
        is a visible, bisectable commit.
    """
    return SEPARATOR.join(load_layers(names))
