# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""The acceptance criteria for `entries-read-from-text`, as tests."""

import pytest

from ledger import Entry, entries_from_text


def test_one_entry_per_non_empty_line() -> None:
    read = entries_from_text("rent,-50000\nwages,120000\n")
    assert read == [Entry("rent", -50_000), Entry("wages", 120_000)]


def test_blank_lines_are_not_entries() -> None:
    assert entries_from_text("rent,-50000\n\n\n") == [Entry("rent", -50_000)]


def test_surrounding_whitespace_is_not_part_of_the_value() -> None:
    assert entries_from_text("  rent , -50000 \n") == [Entry("rent", -50_000)]


def test_a_line_in_the_wrong_shape_names_itself() -> None:
    with pytest.raises(ValueError, match="rent only"):
        entries_from_text("rent only\n")
