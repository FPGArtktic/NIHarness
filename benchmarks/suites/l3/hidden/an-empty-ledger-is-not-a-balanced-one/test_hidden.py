# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""The acceptance criteria for `an-empty-ledger-is-not-a-balanced-one`."""

import pytest

from ledger import Entry, balance


def test_an_empty_ledger_is_refused() -> None:
    with pytest.raises(ValueError, match="no entries"):
        balance([])


def test_entries_that_cancel_still_balance_to_zero() -> None:
    assert balance([Entry("in", 500), Entry("out", -500)]) == 0


def test_a_ledger_with_entries_still_sums() -> None:
    assert balance([Entry("rent", -50_000), Entry("wages", 120_000)]) == 70_000
