# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""What the ledger already guarantees."""

from ledger import Entry, balance, credits, format_cents


def test_balance_sums_every_entry() -> None:
    entries = [Entry("rent", -50_000), Entry("wages", 120_000)]
    assert balance(entries) == 70_000


def test_credits_are_the_positive_entries() -> None:
    entries = [Entry("rent", -50_000), Entry("wages", 120_000)]
    assert credits(entries) == [Entry("wages", 120_000)]


def test_format_cents_keeps_two_places() -> None:
    assert format_cents(1_234) == "12.34"
    assert format_cents(-1_234) == "-12.34"
    assert format_cents(5) == "0.05"
