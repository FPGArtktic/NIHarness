# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""The acceptance criteria for `debits-alongside-credits`, as tests."""

from ledger import Entry, credits, debits


def test_debits_are_the_negative_entries_in_order() -> None:
    entries = [Entry("rent", -50_000), Entry("wages", 120_000), Entry("fees", -1_500)]
    assert debits(entries) == [Entry("rent", -50_000), Entry("fees", -1_500)]


def test_zero_is_neither_a_credit_nor_a_debit() -> None:
    entries = [Entry("adjustment", 0)]
    assert credits(entries) == []
    assert debits(entries) == []
