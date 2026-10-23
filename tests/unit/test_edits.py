# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""Locating an edit by the text it replaces.

The rule is uniqueness, and the value of the rule is in what it says when it
is not met: a model that cannot find its own passage has to be told where the
near miss is, or it will send the same thing again.
"""

import pytest

from niharness.domain.edits import EditOutcome, plan_edit

CLASS = 'class Driver:\n    """One device."""\n\n    def reset(self):\n        return 1\n'


def test_a_passage_that_occurs_once_is_replaced() -> None:
    plan = plan_edit(CLASS, "        return 1", "        return 0")
    assert plan.outcome is EditOutcome.UNIQUE
    assert plan.occurrences == 1
    assert plan.updated.endswith("        return 0\n")
    assert plan.detail == ""


def test_an_empty_replacement_deletes_the_passage() -> None:
    plan = plan_edit(CLASS, '    """One device."""\n', "")
    assert plan.outcome is EditOutcome.UNIQUE
    assert '"""One device."""' not in plan.updated


def test_a_passage_that_occurs_twice_says_how_many_and_what_to_do() -> None:
    """Without the count, the model cannot tell this from "not found"."""
    plan = plan_edit("a = 1\nb = 1\n", "= 1", "= 2")
    assert plan.outcome is EditOutcome.AMBIGUOUS
    assert plan.occurrences == 2
    assert "occurs 2 times" in plan.detail
    assert "unique" in plan.detail, "say what would make it unambiguous"
    assert plan.updated == "", "nothing is written when the target is unclear"


def test_the_wrong_indentation_is_quoted_back_exactly() -> None:
    """The commonest near miss: a block retyped at the wrong depth.

    It is in the file, the model nearly has it, and the answer is the exact
    bytes - not an instruction to go and read the file again.
    """
    plan = plan_edit(CLASS, "def reset(self):\nreturn 1", "x")
    assert plan.outcome is EditOutcome.MISSING
    assert "differs only in whitespace" in plan.detail
    assert "    def reset(self):\n        return 1" in plan.detail


def test_several_whitespace_near_misses_are_not_offered_as_one() -> None:
    """Quoting one back would pick a place the model did not mean."""
    body = "if x:\n    go()\n\nif x:\n        go()\n"
    plan = plan_edit(body, "if x:\ngo()", "stop()")
    assert plan.outcome is EditOutcome.MISSING
    assert "2 places differ" in plan.detail
    assert "ambiguous" in plan.detail
    assert plan.updated == ""


def test_a_passage_that_was_recalled_rather_than_copied_gets_the_closest_line() -> None:
    plan = plan_edit(CLASS, "    def reset(self, hard):", "x")
    assert plan.outcome is EditOutcome.MISSING
    assert "closest line" in plan.detail
    assert "def reset(self):" in plan.detail
    assert "line numbers" in plan.detail, "the other way this goes wrong"


def test_a_passage_with_nothing_like_it_says_so_plainly() -> None:
    """Inventing a suggestion here would send the model after the wrong line."""
    plan = plan_edit(CLASS, "zzzz qqqq wwww", "x")
    assert plan.outcome is EditOutcome.MISSING
    assert "nothing in the file resembles it" in plan.detail


def test_an_edit_that_changes_nothing_is_refused() -> None:
    """Writing it would report success and leave the file as it was."""
    plan = plan_edit(CLASS, "        return 1", "        return 1")
    assert plan.outcome is EditOutcome.UNCHANGED
    assert "change nothing" in plan.detail
    assert plan.updated == ""


def test_a_long_passage_is_truncated_when_quoted_back() -> None:
    body = "".join(f"    line {number}\n" for number in range(200))
    plan = plan_edit(body, "line 0\nline 1", "x")
    assert plan.outcome is EditOutcome.MISSING
    assert len(plan.detail) < 1_200


@pytest.mark.parametrize("find", ["\n", " ", "\n\n"])
def test_whitespace_only_passages_do_not_crash_the_near_miss_search(find: str) -> None:
    plan = plan_edit(CLASS, find, "x")
    assert plan.outcome in {EditOutcome.UNIQUE, EditOutcome.AMBIGUOUS, EditOutcome.MISSING}
