# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""A ledger of entries, each a description and an amount in whole cents."""

from __future__ import annotations

from dataclasses import dataclass

CENTS_IN_A_UNIT = 100
"""Cents per unit of currency, so no amount is ever carried as a float."""


@dataclass(frozen=True)
class Entry:
    """One line of the ledger. Amounts are cents: a credit is positive."""

    description: str
    cents: int


def balance(entries: list[Entry]) -> int:
    """Return the sum of every entry's amount, in cents."""
    return sum(entry.cents for entry in entries)


def credits(entries: list[Entry]) -> list[Entry]:
    """Return the entries that add to the balance."""
    return [entry for entry in entries if entry.cents > 0]


def format_cents(cents: int) -> str:
    """Render an amount as a decimal string, with a sign only when negative.

    Args:
        cents: the amount, in whole cents.

    Returns:
        The amount with two decimal places, for example `-12.34`.
    """
    sign = "-" if cents < 0 else ""
    whole, part = divmod(abs(cents), CENTS_IN_A_UNIT)
    return f"{sign}{whole}.{part:02d}"
