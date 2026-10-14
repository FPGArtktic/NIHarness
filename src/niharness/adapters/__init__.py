# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""Adapters: implementations of the ports.

Every adapter implements one port and is covered by a contract test that runs
against recorded responses, never a live backend. Adapters do not import each
other; wiring happens in `niharness.cli`.
"""
