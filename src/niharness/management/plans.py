# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""The plan store.

A driver that does not share this machine's filesystem conventions - an agent,
say - needs somewhere to put a plan and a way to name it afterwards. Plans are
stored under the state directory by their digest, so naming one is unambiguous
and approving one cannot drift from what was reviewed.
"""

from __future__ import annotations

from pathlib import Path

import structlog

from niharness.config import HarnessConfig
from niharness.domain.artifacts import Plan
from niharness.domain.identity import Digest
from niharness.domain.markdown import parse_plan, render_plan

PLAN_DIR = "plans"

_log = structlog.get_logger(__name__)


class NoSuchPlanError(LookupError):
    """No stored plan has that digest."""


class PlanStore:
    """Stores plans by digest, and records which one is approved."""

    def __init__(self, config: HarnessConfig) -> None:
        """Bind to a project."""
        self._config = config

    @property
    def directory(self) -> Path:
        """Where stored plans live."""
        return self._config.state_dir / PLAN_DIR

    def path_for(self, digest: Digest) -> Path:
        """Where the plan with `digest` is stored."""
        return self.directory / f"{digest}.md"

    def submit(self, markdown: str) -> tuple[Plan, Path]:
        """Validate a plan document and store it.

        Args:
            markdown: a PLAN.md document, or the bare artifact JSON.

        Returns:
            The parsed plan and where it was stored.

        Raises:
            ValueError: if the document does not carry a valid plan. The caller
                reports it; the store does not guess at a repair.
        """
        plan = parse_plan(markdown)
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.path_for(plan.digest())
        path.write_text(render_plan(plan), encoding="utf-8")
        _log.info("plan.stored", digest=str(plan.digest()), tasks=len(plan.tasks))
        return plan, path

    def load(self, digest: Digest) -> tuple[Plan, Path]:
        """Return a stored plan.

        Raises:
            NoSuchPlanError: when nothing is stored under that digest.
        """
        path = self.path_for(digest)
        if not path.is_file():
            raise NoSuchPlanError(str(digest))
        return parse_plan(path.read_text(encoding="utf-8")), path

    def approve(self, digest: Digest) -> Path:
        """Mark a stored plan as the one `nih implement` runs.

        Raises:
            NoSuchPlanError: when nothing is stored under that digest.
        """
        plan, _ = self.load(digest)
        target = self._config.approved_plan_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(render_plan(plan), encoding="utf-8")
        _log.info("plan.approved", digest=str(digest), path=str(target))
        return target

    def approved(self) -> Plan | None:
        """Return the approved plan, or None when there is none."""
        path = self._config.approved_plan_path
        if not path.is_file():
            return None
        return parse_plan(path.read_text(encoding="utf-8"))
