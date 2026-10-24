<!--
SPDX-FileCopyrightText: 2026 Mateusz Okulanis
SPDX-License-Identifier: GPL-3.0-or-later
-->

# ledger

A small ledger: entries in whole cents, a balance, and a formatter. It exists
for the L3 benchmark to give a model whole tasks to do, so it is deliberately
plain - one module, one test file, no dependencies beyond the standard library.

Amounts are **always cents**, never floats. A task that introduces a float
amount has misread the module.
