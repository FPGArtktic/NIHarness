# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""Reading a diff the way a model writes one.

Every case here is a diff a live model actually produced, or the correct diff
it should have produced. The rules under test are narrow on purpose: the
paperwork of a creation is repaired, `read_file`'s own line numbers are taken
off because the harness put them there, a diff that changes nothing is named
as such, and nothing else is touched.
"""

from collections.abc import Callable

import pytest

from niharness.domain.diffs import (
    changes_nothing,
    ensure_terminated,
    is_not_a_diff,
    repair_new_file_headers,
    strip_line_numbers,
)

NOTHING_EXISTS: frozenset[str] = frozenset()


def exists_in(paths: frozenset[str]) -> Callable[[str], bool]:
    def predicate(path: str) -> bool:
        return path in paths

    return predicate


CREATION = (
    "--- src/greeter/greet.py\n"
    "+++ src/greeter/greet.py\n"
    "@@ -0,0 +1,3 @@\n"
    '+"""Greeting."""\n'
    "+\n"
    "+VALUE = 1\n"
)


def test_a_creation_a_model_wrote_is_repaired_and_reported() -> None:
    repaired, created = repair_new_file_headers(CREATION, exists_in(NOTHING_EXISTS))
    assert created == ("src/greeter/greet.py",)
    assert repaired.splitlines()[0] == "--- /dev/null"
    # Both headers must agree with the strip level git applies by default, or
    # the file is created one directory too high.
    assert repaired.splitlines()[1] == "+++ b/src/greeter/greet.py"


def test_a_creation_written_correctly_is_left_alone() -> None:
    correct = CREATION.replace("--- src/greeter/greet.py", "--- /dev/null").replace(
        "+++ src/greeter/greet.py", "+++ b/src/greeter/greet.py"
    )
    repaired, created = repair_new_file_headers(correct, exists_in(NOTHING_EXISTS))
    assert repaired == correct
    assert created == ()


def test_a_diff_against_an_existing_file_is_never_read_as_a_creation() -> None:
    # The predicate is the whole safety: if the file is there, a `-0,0` hunk is
    # the model's mistake to hear about, not ours to reinterpret.
    repaired, created = repair_new_file_headers(
        CREATION,
        exists_in(frozenset({"src/greeter/greet.py"})),
    )
    assert repaired == CREATION
    assert created == ()


def test_a_modifying_diff_is_untouched() -> None:
    modification = (
        '--- a/src/app.py\n+++ b/src/app.py\n@@ -1,2 +1,3 @@\n """App."""\n \n+VALUE = 1\n'
    )
    repaired, created = repair_new_file_headers(modification, exists_in(NOTHING_EXISTS))
    assert repaired == modification
    assert created == ()


def test_several_files_in_one_diff_are_each_handled() -> None:
    both = CREATION + (
        '--- tests/test_greet.py\n+++ tests/test_greet.py\n@@ -0,0 +1,2 @@\n+"""Tests."""\n+\n'
    )
    repaired, created = repair_new_file_headers(both, exists_in(NOTHING_EXISTS))
    assert created == ("src/greeter/greet.py", "tests/test_greet.py")
    assert repaired.count("--- /dev/null") == 2


def test_a_diff_with_no_trailing_newline_keeps_not_having_one() -> None:
    repaired, _ = repair_new_file_headers(CREATION.rstrip("\n"), exists_in(NOTHING_EXISTS))
    assert not repaired.endswith("\n")


def test_nothing_is_read_as_a_creation_without_a_zero_hunk() -> None:
    headers_only = "--- src/new.py\n+++ src/new.py\n"
    repaired, created = repair_new_file_headers(headers_only, exists_in(NOTHING_EXISTS))
    assert repaired == headers_only
    assert created == ()


def test_an_unterminated_last_line_is_terminated() -> None:
    # A model writing a diff as a string argument drops the final newline
    # constantly, and git then reads the hunk as truncated and calls the whole
    # patch corrupt at a line that looks fine.
    terminated, repaired = ensure_terminated("--- /dev/null\n+++ b/a.py\n@@ -0,0 +1 @@\n+x = 1")
    assert repaired
    assert terminated.endswith("+x = 1\n")


def test_a_terminated_diff_is_left_exactly_as_it_is() -> None:
    diff = "--- /dev/null\n+++ b/a.py\n@@ -0,0 +1 @@\n+x = 1\n"
    assert ensure_terminated(diff) == (diff, False)


# ------------------------------------------------- read_file's own line numbers

NUMBERED = (
    "--- src/clk9000.py\n"
    "+++ src/clk9000.py\n"
    "@@ -1,1 +1,3 @@\n"
    '      1  """Driver constants."""\n'
    "+\n"
    "+VDDC_VOLTS = 1.20\n"
)


def test_line_numbers_copied_out_of_read_file_are_removed() -> None:
    """`read_file` numbers its answer; the numbers are not in the file.

    A diff that keeps them has context matching nothing, and git's complaint
    names a hunk header rather than the cause. The harness added the numbers,
    so it takes them off.
    """
    repaired, stripped = strip_line_numbers(
        "--- a.py\n+++ a.py\n@@ -1,2 +1,2 @@\n-     1  old = 1\n+     2  new = 2\n",
    )
    assert stripped
    assert repaired == "--- a.py\n+++ a.py\n@@ -1,2 +1,2 @@\n-old = 1\n+new = 2\n"


def test_a_margin_written_loosely_is_still_recognised() -> None:
    """A model reformats what it read; the shape survives, the spacing does not."""
    repaired, stripped = strip_line_numbers(
        "--- a.py\n+++ a.py\n@@ -1,2 +1,2 @@\n 1 first\n 2 second\n",
    )
    assert stripped
    assert repaired.endswith(" first\n second\n")


def test_one_numbered_looking_line_is_a_coincidence() -> None:
    repaired, stripped = strip_line_numbers("--- a.py\n+++ a.py\n@@ -1 +1 @@\n+1 thing\n")
    assert (repaired, stripped) == ("--- a.py\n+++ a.py\n@@ -1 +1 @@\n+1 thing\n", False)


def test_numbers_that_go_backwards_are_not_a_margin() -> None:
    """A left margin counts upwards. Data does whatever it likes."""
    diff = "--- a.txt\n+++ a.txt\n@@ -1,2 +1,2 @@\n-9 nine\n+3 three\n"
    assert strip_line_numbers(diff) == (diff, False)


def test_a_file_of_numbers_is_left_alone() -> None:
    """The repair must not eat a CSV, a table, or a list of measurements."""
    diff = "--- data.csv\n+++ data.csv\n@@ -1,2 +1,2 @@\n-1,2,3\n+4,5,6\n"
    assert strip_line_numbers(diff) == (diff, False)


def test_the_headers_are_never_mistaken_for_body_lines() -> None:
    """`--- ` and `+++ ` start with a body prefix and are not body lines."""
    repaired, _ = strip_line_numbers(NUMBERED)
    assert repaired.startswith("--- src/clk9000.py\n+++ src/clk9000.py\n")


# ------------------------------------------------------- a diff that does nothing


def test_a_diff_of_nothing_but_context_is_recognised() -> None:
    """Valid, applies cleanly, and changes nothing.

    Observed on a live run: the model wrote its new lines with a leading
    space, as though quoting the file it wanted. git would have reported
    success and left the file untouched.
    """
    assert changes_nothing(
        "--- a.py\n+++ a.py\n@@ -1,2 +1,2 @@\n x = 1\n y = 2\n",
    )


@pytest.mark.parametrize(
    "diff",
    [
        "--- a.py\n+++ a.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n",
        "--- /dev/null\n+++ b/a.py\n@@ -0,0 +1 @@\n+x = 1\n",
        "--- a.py\n+++ a.py\n@@ -1,2 +1,1 @@\n x = 1\n-y = 2\n",
    ],
)
def test_a_diff_that_adds_or_removes_a_line_does_something(diff: str) -> None:
    assert not changes_nothing(diff)


def test_headers_without_a_hunk_are_not_a_change_that_does_nothing() -> None:
    """There is nothing to refuse yet; git's own message is the better one."""
    assert not changes_nothing("--- a.py\n+++ a.py\n")


@pytest.mark.parametrize(
    "diff",
    [
        "+ # a comment\n+ VALUE = 1\n",
        "--- a.py\n+++ a.py\n",
        "@@ -1 +1 @@\n-x = 1\n+x = 2\n",
        "VALUE = 1\n",
    ],
)
def test_something_that_is_not_a_diff_is_recognised_before_git_sees_it(diff: str) -> None:
    """What the live 4B sent after being told its context-only diff did nothing.

    It moved its lines to `+`, which was the correction asked for, and dropped
    the headers. "corrupt patch" does not say which part is missing.
    """
    assert is_not_a_diff(diff)


def test_a_complete_diff_is_not_mistaken_for_a_fragment() -> None:
    assert not is_not_a_diff("--- a.py\n+++ b/a.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n")
    assert not is_not_a_diff("--- /dev/null\n+++ b/a.py\n@@ -0,0 +1 @@\n+x = 1\n")
