# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""Bounded views over a run.

Every answer this layer gives is read by something that pays for it - a person
scrolling a terminal, or an agent spending context on each line. So nothing
here returns a log: it returns a digest, built from the same primitives the
harness already uses to keep tool results small, and says when it truncated.
"""

from __future__ import annotations

from collections.abc import Sequence

from niharness.domain import events as ev
from niharness.domain.artifacts import Plan
from niharness.domain.events import Event
from niharness.domain.state import RunSnapshot
from niharness.domain.tools import ToolLimits, truncate_result
from niharness.management.runs import RunEntry

BUDGET = ToolLimits(max_result_bytes=4_000)
"""What one management answer may cost. Four kilobytes is roughly a thousand tokens."""

DIFF_BUDGET = ToolLimits(max_result_bytes=8_000)
"""A diff is the one answer worth paying more for; it is what review is about."""

DEFAULT_EVENT_LIMIT = 20
"""How many recent events a digest shows unless asked otherwise."""


def bounded(text: str, limits: ToolLimits = BUDGET) -> str:
    """Return `text` within budget, saying so when it was cut."""
    content, truncated = truncate_result(text, limits)
    return f"{content}\n[truncated to fit the answer budget]" if truncated else content


def render_runs(entries: Sequence[RunEntry]) -> str:
    """Render the registry."""
    if not entries:
        return "no runs"
    lines = [f"{len(entries)} run(s):"]
    for entry in entries:
        mark = "live" if entry.live else "done" if entry.outcome != "running" else "idle"
        lines.append(
            f"  {entry.run_id}  [{mark}] {entry.outcome}, stage {entry.stage}, "
            f"{entry.events} events, {entry.commits} commit(s)",
        )
    return bounded("\n".join(lines))


def render_status(entry: RunEntry, snapshot: RunSnapshot) -> str:
    """Render one run's state: what it is doing and what it has cost."""
    lines = [
        f"run {snapshot.run_id}: {entry.outcome}" + (" (live)" if entry.live else ""),
        f"  stage: {snapshot.stage or '-'}"
        + (f", task {snapshot.current_task}" if snapshot.current_task else ""),
        f"  events: {snapshot.seq}, tokens: {snapshot.tokens_used}",
        f"  tools: {snapshot.tool_calls} called, {snapshot.rejected_calls} rejected",
        f"  gate: {snapshot.gate_runs} run(s), {snapshot.gate_failures} red",
        f"  commits: {len(snapshot.commits)}",
        f"  compactions: {snapshot.compactions}, context fill {snapshot.context_fill:.0%}",
    ]
    if snapshot.highest_intervention:
        lines.append(f"  highest loop intervention: level {snapshot.highest_intervention}")
    if snapshot.open_questions:
        lines.append(f"  waiting on {snapshot.open_questions} question(s)")
    if snapshot.last_failure_signature:
        lines.append(f"  last gate failure signature: {snapshot.last_failure_signature}")
    return bounded("\n".join(lines))


def render_tasks(snapshot: RunSnapshot) -> str:
    """Render what happened to each task of the plan."""
    if not snapshot.tasks:
        return "no task has started yet"
    lines = [f"{len(snapshot.tasks)} task(s) seen in run {snapshot.run_id}:"]
    for task in snapshot.tasks:
        mark = "FAIL" if task.failed else "ok  "
        detail = (
            f"{task.steps} step(s), {len(task.commits)} commit(s), stage {task.stage or '-'}"
        )
        lines.append(f"  [{mark}] {task.task_id}: {detail}")
        if task.reason:
            lines.append(f"         {task.reason}")
    return bounded("\n".join(lines))


def _detail(body: ev.EventBody) -> str:  # noqa: C901 - one branch per event type
    """Return the part of an event worth a line."""
    match body:
        case ev.StepStarted():
            return f"{body.stage} as {body.role}" + (
                f" [{body.task_id}]" if body.task_id else ""
            )
        case ev.StepFinished():
            return f"{body.stop_reason.value}, {body.tokens_used} tokens"
        case ev.StepFailed():
            return body.reason
        case ev.ToolCalled():
            return body.name
        case ev.ToolReturned():
            return ("ok " if body.ok else "RED ") + body.summary
        case ev.ToolRejected():
            return f"{body.name}: {body.violation}"
        case ev.GateFinished():
            return f"{body.tier} {'green' if body.passed else 'RED'}"
        case ev.VcsCommitted():
            return f"{body.sha[:12]} {body.subject}"
        case ev.LoopIntervention():
            return f"level {int(body.level)} ({body.pathology})"
        case ev.ContextCompacted():
            return f"{body.before_tokens} -> {body.after_tokens} tokens"
        case ev.RunFinished():
            return f"{body.outcome.value}: {body.reason}"
        case ev.RunCancelled():
            return f"from {body.source}"
        case _:
            return ""


def event_line(event: Event) -> str:
    """Render one event as a line worth the tokens it costs."""
    return f"{event.seq:>5} {event.type:20} {_detail(event.body)}".rstrip()


def render_events(
    events: Sequence[Event],
    *,
    since: int = 0,
    limit: int = DEFAULT_EVENT_LIMIT,
    kinds: Sequence[str] = (),
) -> tuple[str, int]:
    """Render recent events and return the cursor to pass next time.

    Args:
        events: the run's events, in order.
        since: only consider events after this sequence number.
        limit: how many lines to render, newest last.
        kinds: event types to keep; everything when empty.

    Returns:
        The digest and the sequence number to pass as `since` next time.
    """
    fresh = [event for event in events if event.seq > since]
    if kinds:
        wanted = set(kinds)
        fresh = [event for event in fresh if event.type in wanted]
    cursor = max((event.seq for event in fresh), default=since)
    if not fresh:
        return ("nothing new", cursor)
    counts: dict[str, int] = {}
    for event in fresh:
        counts[event.type] = counts.get(event.type, 0) + 1
    summary = ", ".join(f"{name} x{count}" for name, count in sorted(counts.items()))
    shown = fresh[-limit:]
    lines = [
        f"{len(fresh)} event(s) since {since}: {summary}",
        f"last {len(shown)}:",
        *[event_line(event) for event in shown],
    ]
    return (bounded("\n".join(lines)), cursor)


def render_plan(plan: Plan) -> str:
    """Render a plan as the task list a supervisor needs to see."""
    lines = [
        f"plan {plan.digest()}: {len(plan.tasks)} task(s), spec {plan.spec_digest}",
    ]
    lines += [
        f"  {index}. {task.id}: {task.title}"
        + (f" (after {', '.join(task.depends_on)})" if task.depends_on else "")
        for index, task in enumerate(plan.tasks, start=1)
    ]
    if plan.open_questions:
        lines += ["open questions:", *[f"  - {item}" for item in plan.open_questions]]
    return bounded("\n".join(lines))
