# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""Rewrite the prompt snapshots from the shipped layers.

Run this deliberately, as part of the commit that changes a prompt:

    uv run python -m tests.golden.regenerate
"""

from pathlib import Path

from niharness.domain.roles import DEFAULT_ROLES
from niharness.prompts import assemble

GOLDEN = Path(__file__).parent / "prompts"


def main() -> None:
    """Write one snapshot per role."""
    GOLDEN.mkdir(parents=True, exist_ok=True)
    for role, spec in DEFAULT_ROLES.items():
        (GOLDEN / f"{role.value}.md").write_text(
            assemble(spec.prompt_layers),
            encoding="utf-8",
        )


if __name__ == "__main__":
    main()
