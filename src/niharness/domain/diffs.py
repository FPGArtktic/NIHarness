# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""Reading a unified diff the way a model writes one.

`git apply` is exact, and a model writing a diff by hand gets the *change*
right far more often than it gets the paperwork right. Four clerical mistakes
account for nearly all of it: the `a/` and `b/` path prefixes, hunk line counts
counted by eye, line numbers copied out of `read_file`'s own output, and - the
one that cannot be fixed with a git option - a new file announced as

    --- src/new.py
    +++ src/new.py
    @@ -0,0 +1,3 @@

where git requires `--- /dev/null`, because a hunk that starts at line zero of
an existing file is a contradiction and git says so by refusing to find the
file.

That intent is unambiguous: a hunk beginning `@@ -0,0` creates a file. So the
header is repaired here, and the repair is reported back to the model, because
a tool that silently fixes its caller's mistakes teaches it nothing.

The line numbers are this harness's own doing. `read_file` answers with
numbered lines, which is what makes it readable; a model that then writes a
diff from what it read carries the numbers into the context lines, where they
are not in the file. Every line of such a diff mismatches, and git's complaint
names a hunk header rather than the cause. Since the harness added the
numbers, it can take them off again - and does, as another reported repair.

Nothing in this module changes what a diff *does*. The content still has to
match the file, which is what the gate and the reviewer are for.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from itertools import pairwise

DEV_NULL = "/dev/null"
"""What the `---` line of a creating diff has to say."""

NEW_FILE_HUNK = re.compile(r"^@@ -0,0[ ,+]")
"""A hunk that starts at line zero: the file is being created."""

PREFIXES: tuple[str, ...] = ("a/", "b/")
"""Path prefixes `git apply` strips by default; a model often omits them."""

BODY_PREFIXES: frozenset[str] = frozenset({" ", "+", "-"})
"""What a line inside a hunk starts with: context, added, removed."""


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


LINE_NUMBER = re.compile(r"^\s{0,8}(\d{1,7})(?:\t| {1,2})")
"""`read_file`'s own left margin: a number, then the separator, then the line.

The separator is what `read_file` writes - `f"{number:>6}  {line}"` - loosened
to one space or a tab, because a model reformats what it read. The line's own
indentation follows and is kept: stripping it would turn a diff of Python into
one that does not match the file either.

Narrow on purpose everywhere else. A digit followed by anything but the
separator - `1,2,3` in a CSV, `1+2` in an expression - is not a margin, and
the caller requires several of these before believing any of them.
"""

MIN_NUMBERED_LINES = 2
"""One numbered-looking line is a coincidence; two that count upwards are not."""


def strip_line_numbers(diff: str) -> tuple[str, bool]:
    """Return the diff with `read_file`'s line numbers taken off its body lines.

    Only attempted when the evidence is unambiguous: at least two body lines
    carry what looks like a left margin, and the numbers do not go backwards.
    Whether the result is *right* is not decided here - the caller applies it,
    and a wrong guess simply does not apply, which is the same answer the
    unrepaired diff got.

    Args:
        diff: the diff as the model wrote it.

    Returns:
        The diff with the margins removed, and whether anything was removed.
    """
    lines = diff.splitlines(keepends=True)
    found: list[tuple[int, int, str]] = []
    for index, line in enumerate(lines):
        if not line[:1] or line[0] not in BODY_PREFIXES:
            continue
        body = line[1:].rstrip("\n")
        match = LINE_NUMBER.match(body)
        if match is None:
            continue
        found.append((index, int(match.group(1)), line[0] + body[match.end() :]))
    if len(found) < MIN_NUMBERED_LINES:
        return diff, False
    numbers = [number for _, number, _ in found]
    if any(later < earlier for earlier, later in pairwise(numbers)):
        return diff, False
    for index, _, stripped in found:
        lines[index] = stripped + ("\n" if lines[index].endswith("\n") else "")
    return "".join(lines), True


def changes_nothing(diff: str) -> bool:
    """Return whether a diff has hunks but not one added or removed line.

    A diff made entirely of context lines is valid, applies cleanly when the
    context matches, and does nothing - so the tool would answer "applied" to
    a model that had written no change at all. Observed on a live run: the
    model wrote its new lines with a leading space, as though quoting the file
    it wanted.

    Refusing it is better than applying it, because the failure is otherwise
    invisible until the gate or the reviewer finds the file untouched.

    A diff that is malformed in some *other* way - a line with no prefix at
    all - is not reported here. git names that line, and a specific complaint
    beats a general one.
    """
    hunks = False
    context = 0
    for line in diff.splitlines():
        if line.startswith("@@"):
            hunks = True
            continue
        if not hunks or line.startswith(("+++ ", "--- ")):
            continue
        if line[:1] in {"+", "-"}:
            return False
        if line and line[0] not in {" ", "\\"}:
            # A line with no prefix at all is a different mistake, and git's
            # own complaint about it names the line. Saying "this changes
            # nothing" instead would be true and less useful.
            return False
        context += 1
    return hunks and context > 0


def is_not_a_diff(diff: str) -> bool:
    """Return whether this is not a unified diff at all.

    A model that has been told its context-only diff changes nothing will
    sometimes send the added lines on their own - `+ VALUE = 1` and nothing
    else. git calls that a corrupt patch; what it is missing is the two header
    lines and a hunk, and saying so is shorter than the rule it broke.
    """
    lines = diff.splitlines()
    return not any(line.startswith("@@") for line in lines) or not any(
        line.startswith("--- ") for line in lines
    )
