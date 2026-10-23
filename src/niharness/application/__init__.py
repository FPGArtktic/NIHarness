# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""The application layer: services that drive the domain through the ports.

This is the only layer that is allowed to be boring. It owns no rules - the
domain decides what happens next - and it owns no protocol details - the
adapters do. It performs actions, feeds the outcome back, and appends the
events it is handed.
"""

from niharness.application.implement import ImplementationRun, RunResult, TaskResult
from niharness.application.step_runner import RunnerLimitError, StepRunner
from niharness.application.summarizer import ModelSummarizer, Summarizer
from niharness.application.window import (
    ContextArchive,
    MeasurementMissingError,
    Window,
    WindowSnapshot,
)

__all__ = [
    "ContextArchive",
    "ImplementationRun",
    "MeasurementMissingError",
    "ModelSummarizer",
    "RunResult",
    "RunnerLimitError",
    "StepRunner",
    "Summarizer",
    "TaskResult",
    "Window",
    "WindowSnapshot",
]
