# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""Reading a unified diff the way a model writes one.

`git apply` is exact, and a model writing a diff by hand gets the *change*
right far more often than it gets the paperwork right. Three clerical mistakes
account for nearly all of it: the `a/` and `b/` path prefixes, hunk line counts
counted by eye, and - the one that cannot be fixed with a git option - a new
file announced as

    --- src/new.py
    +++ src/new.py
    @@ -0,0 +1,3 @@

where git requires `--- /dev/null`, because a hunk that starts at line zero of
an existing file is a contradiction and git says so by refusing to find the
file.

That intent is unambiguous: a hunk beginning `@@ -0,0` creates a file. So the
header is repaired here, and the repair is reported back to the model, because
a tool that silently fixes its caller's mistakes teaches it nothing.

Nothing in this module changes what a diff *does*. The content still has to
match the file, which is what the gate and the reviewer are for.
"""

from __future__ import annotations

import re
from collections.abc import Callable

DEV_NULL = "/dev/null"
"""What the `---` line of a creating diff has to say."""

NEW_FILE_HUNK = re.compile(r"^@@ -0,0[ ,+]")
"""A hunk that starts at line zero: the file is being created."""

PREFIXES: tuple[str, ...] = ("a/", "b/")
"""Path prefixes `git apply` strips by default; a model often omits them."""


def strip_prefix(path: str) -> str:
    """Return a diff header's path without a leading `a/` or `b/`."""
    for prefix in PREFIXES:
        if path.startswith(prefix):
            return path[len(prefix) :]
    return path


def header_path(line: str) -> str:
    """Return the path a `---` or `+++` line names, without its timestamp."""
    rest = line[4:].strip()
    return rest.split("\t", 1)[0].strip()


def ensure_terminated(diff: str) -> tuple[str, bool]:
    """Return the diff with its last line terminated, and whether that was needed.

    A hunk's last line has to end with a newline or git reads the hunk as
    truncated and calls the whole patch corrupt - pointing at a line that looks
    fine, because the problem is the character that is not there. A model
    writing the diff as a string argument drops it constantly: observed on a
    live run, where the diff ended with a bare `+` for an empty added line.
    """
    if diff.endswith("\n"):
        return diff, False
    return diff + "\n", True


def repair_new_file_headers(
    diff: str,
    exists: Callable[[str], bool],
) -> tuple[str, tuple[str, ...]]:
    """Return the diff with creating headers git will accept.

    Args:
        diff: the diff as the model wrote it.
        exists: whether a worktree-relative path is an existing file. A header
            is only rewritten when the file is *not* there, so a diff against
            real content is never reinterpreted as a creation.

    Returns:
        The diff, and the paths whose `---` line was rewritten - for the
        message that tells the model what it got wrong.
    """
    lines = diff.splitlines()
    repaired: list[str] = []
    source: int | None = None
    sink: int | None = None
    target = ""
    for index, line in enumerate(lines):
        if line.startswith("--- "):
            source, sink, target = index, None, ""
            continue
        if line.startswith("+++ ") and source is not None:
            sink = index
            target = strip_prefix(header_path(line))
            continue
        if not line.startswith("@@") or source is None or sink is None:
            continue
        if NEW_FILE_HUNK.match(line) and target and not exists(target):
            named = header_path(lines[source])
            if named != DEV_NULL:
                lines[source] = f"--- {DEV_NULL}"
                # Both headers have to agree with the strip level git applies
                # by default, or the file is created one directory too high:
                # `+++ src/new.py` under `-p1` becomes `new.py`.
                if header_path(lines[sink]) == target:
                    lines[sink] = f"+++ b/{target}"
                repaired.append(target)
        source = None
    return "\n".join(lines) + ("\n" if diff.endswith("\n") else ""), tuple(repaired)
