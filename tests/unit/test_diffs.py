# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""Reading a diff the way a model writes one.

Every case here is a diff a live model actually produced, or the correct diff
it should have produced. The rule under test is narrow on purpose: the
paperwork of a creation is repaired, and nothing else is.
"""

from collections.abc import Callable

from niharness.domain.diffs import repair_new_file_headers

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
