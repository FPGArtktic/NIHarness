<!--
SPDX-FileCopyrightText: 2026 Mateusz Okulanis
SPDX-License-Identifier: GPL-3.0-or-later
-->

# API: application

Services that drive the domain through the ports. They own no rules and no
protocol details: they perform the action the domain asked for, feed the
outcome back, and append the events they are handed.

::: niharness.application.step_runner
