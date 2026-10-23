# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""Locating an edit by the text it replaces.

`apply_patch` asks a model for a unified diff, which is an unforgiving
encoding of something the model usually knows perfectly well. Observed on one
live run: a 4B read a supply voltage out of a datasheet, knew the constant it
wanted, and then failed three times to express that one line as a diff - once
with `read_file`'s line numbers in the context, once with every line marked as
context, once with no headers at all.

This is the other way round. The model says which passage to replace and what
to put there; the harness finds it, checks that there is exactly one, writes
the file and *computes* the diff. What reaches the reviewer and the commit is
still a diff, and now it is a well-formed one by construction. Reviewability
is a property of what comes out, not of what the model has to type.

Everything here is pure, so the rules - uniqueness, what a near miss looks
like, when an edit is a no-op - are testable without a worktree.
"""

from __future__ import annotations

from collections.abc import Sequence
from difflib import get_close_matches
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

MAX_QUOTED = 400
"""How much of a passage is quoted back. Enough to recognise, not a file."""

NEAR_MISS_CUTOFF = 0.6
"""`difflib` similarity below which a line is not worth suggesting."""

MAX_CONTEXT_LINES = 14
"""How far a quoted passage may be grown while looking for what distinguishes it."""

MAX_OCCURRENCES_SHOWN = 3
"""How many of an ambiguous passage's occurrences are quoted back."""


class EditOutcome(StrEnum):
    """What locating the passage found."""

    UNIQUE = "unique"
    MISSING = "missing"
    AMBIGUOUS = "ambiguous"
    UNCHANGED = "unchanged"


class EditPlan(BaseModel):
    """The result of locating one edit: what to write, or why nothing can be."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: EditOutcome
    occurrences: int = Field(default=0, ge=0)
    updated: str = Field(
        default="",
        description="The file's new text; meaningful only when the outcome is unique.",
    )
    detail: str = Field(
        default="",
        description="What to tell the model when the edit cannot be made.",
    )


def plan_edit(text: str, find: str, replace: str) -> EditPlan:
    """Return how to replace `find` with `replace` in `text`, or why not.

    Args:
        text: the file's current content.
        find: the exact passage to replace.
        replace: what to put in its place; empty deletes the passage.

    Returns:
        A plan whose outcome is `unique` when the file can be written, and
        which otherwise carries the sentence the model needs to read.
    """
    occurrences = text.count(find)
    if occurrences > 1:
        return EditPlan(
            outcome=EditOutcome.AMBIGUOUS,
            occurrences=occurrences,
            detail=_ambiguous(text, find, occurrences),
        )
    if occurrences == 0:
        return EditPlan(outcome=EditOutcome.MISSING, detail=_missing(text, find))
    if find == replace:
        return EditPlan(
            outcome=EditOutcome.UNCHANGED,
            occurrences=1,
            detail=(
                "`find` and `replace` are the same text, so this would change "
                "nothing. Send the replacement you intend, or read the file if you "
                "are unsure what is in it."
            ),
        )
    return EditPlan(
        outcome=EditOutcome.UNIQUE,
        occurrences=1,
        updated=text.replace(find, replace, 1),
    )


def _ambiguous(text: str, find: str, occurrences: int) -> str:
    """Explain an ambiguous passage by quoting what tells the occurrences apart.

    "Include a line above or below it that is unique" is advice that is
    sometimes *wrong*: two methods of two classes can have identical bodies,
    identical signatures and identical docstrings, so the nearest
    distinguishing line is five lines up and neither neighbour helps. A 4B
    failed this on every pass of the L1 suite, which is deterministic enough
    to be the message's fault rather than the model's.

    The harness knows where the occurrences are, so it can grow each one line
    by line until they differ and hand back the exact text to copy. That is
    mechanical work, which is the harness's half of this bargain.
    """
    quoted = _distinguished(text, find)
    head = f"that passage occurs {occurrences} times, so it does not say which one to change."
    if not quoted:
        return (
            f"{head} Include a line above or below it that is unique - the enclosing "
            "def, the preceding comment - and send the whole thing."
        )
    shown = "\n\n".join(
        f"at line {line}:\n{_quote(passage)}"
        for line, passage in quoted[:MAX_OCCURRENCES_SHOWN]
    )
    more = (
        f"\n(and {len(quoted) - MAX_OCCURRENCES_SHOWN} more)"
        if len(quoted) > MAX_OCCURRENCES_SHOWN
        else ""
    )
    return (
        f"{head} Here is each one grown until they differ - send whichever you meant, "
        f"copied as it is:\n\n{shown}{more}"
    )


def _distinguished(text: str, find: str) -> tuple[tuple[int, str], ...]:
    """Return each occurrence grown until the quoted passages are all different.

    Grows symmetrically, a line at a time, and gives up at
    `MAX_CONTEXT_LINES` - at which point the occurrences really are
    indistinguishable by context and the caller falls back to advice.
    """
    lines = text.splitlines(keepends=True)
    starts = _occurrence_lines(lines, find)
    if len(starts) < 2:
        return ()
    span = len(find.splitlines()) or 1
    # Upwards only. In code the identifying context is above - the enclosing
    # class, the def, the comment that says which of two identical bodies this
    # one is - and growing downwards instead finds differences for the wrong
    # reason: one occurrence running into the *next* declaration, or simply
    # hitting the end of the file. Growing up also leaves the passage at the
    # end of the quote, which reads as "context, then the line I meant".
    for grown in range(0, MAX_CONTEXT_LINES):
        windows = [(max(0, start - grown), start + span) for start in starts]
        # Distinctness is tested on the form that will be *shown*, not on the
        # raw slice: two occurrences that differ only in a trailing newline -
        # one of them being the end of the file - would otherwise be reported
        # as distinguished while looking identical to the reader.
        passages = ["".join(lines[first:last]).rstrip("\n") for first, last in windows]
        if len(set(passages)) == len(passages):
            return tuple(
                (first + 1, passage)
                for (first, _), passage in zip(windows, passages, strict=True)
            )
    return ()


def _occurrence_lines(lines: Sequence[str], find: str) -> tuple[int, ...]:
    """Return the zero-based line of each occurrence of `find`.

    Found by offset rather than by matching lines, so a passage that starts
    mid-line is still located at the line it starts on.
    """
    joined = "".join(lines)
    offsets: list[int] = []
    at = joined.find(find)
    while at != -1:
        offsets.append(at)
        at = joined.find(find, at + 1)
    found: list[int] = []
    for offset in offsets:
        found.append(joined.count("\n", 0, offset))
    return tuple(found)


def _missing(text: str, find: str) -> str:
    """Explain a passage that is not in the file, as usefully as possible.

    "Not found" is true and nearly useless. Two near misses are worth
    checking for, because both are what a model actually produces: a passage
    whose indentation is wrong, which is right there in the file and can be
    quoted back exactly, and a passage recalled rather than copied, where the
    closest line in the file is the hint.
    """
    whitespace = _whitespace_matches(text, find)
    if len(whitespace) == 1:
        return (
            "that passage is not in the file *exactly*, but one place differs only in "
            "whitespace. Send this, copied as it is:\n"
            f"{_quote(whitespace[0])}"
        )
    if len(whitespace) > 1:
        return (
            f"that passage is not in the file exactly; {len(whitespace)} places differ "
            "from it only in whitespace, so even ignoring indentation it would be "
            "ambiguous. Read the file and copy one of them with a unique line around it."
        )
    nearest = _nearest_line(text, find)
    if nearest:
        return (
            "that passage is not in the file. The closest line in it is:\n"
            f"{_quote(nearest)}\n"
            "Copy the text out of `read_file`'s output rather than retyping it - but "
            "without the line numbers it prints, which are not in the file."
        )
    return (
        "that passage is not in the file, and nothing in the file resembles it. Read "
        "the file, or check the path."
    )


def _whitespace_matches(text: str, find: str) -> tuple[str, ...]:
    """Return the file's passages that equal `find` but for whitespace per line.

    Matched line by line on stripped content, because the mistake being caught
    is indentation: a model that re-types a block inside a class body puts it
    at the wrong depth and is otherwise correct.
    """
    wanted = [line.strip() for line in find.splitlines()]
    if not wanted:
        return ()
    lines = text.splitlines(keepends=True)
    found: list[str] = []
    for start in range(len(lines) - len(wanted) + 1):
        window = lines[start : start + len(wanted)]
        if [line.strip() for line in window] != wanted:
            continue
        passage = "".join(window)
        found.append(passage if find.endswith("\n") else passage.rstrip("\n"))
    return tuple(found)


def _nearest_line(text: str, find: str) -> str:
    """Return the file's line most like `find`'s first line, or nothing."""
    first = next((line for line in find.splitlines() if line.strip()), "").strip()
    if not first:
        return ""
    candidates = [line for line in text.splitlines() if line.strip()]
    close = get_close_matches(first, candidates, n=1, cutoff=NEAR_MISS_CUTOFF)
    return close[0] if close else ""


def _quote(passage: str) -> str:
    """Return a passage fenced for reading, truncated if it is long."""
    body = passage if len(passage) <= MAX_QUOTED else f"{passage[:MAX_QUOTED]}\n... [cut]"
    return f"```\n{body}\n```"
