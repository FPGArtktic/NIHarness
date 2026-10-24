# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""`nih bench`: running a benchmark layer and recording what it measured.

L1 asks whether a model can call tools. It runs the suite's tasks through the
**same** loop that runs real work - the same validation, the same detectors,
the same budgets - and then scores each task by folding that run's events. Two
things follow from doing it that way. A number here is about the model rather
than about a test rig that happens to resemble the harness, and the scoring
needs no model, so it is unit-tested on recorded event sequences.

Each task gets a fresh copy of the suite's `fixture/`, made into a git
repository of its own. The tasks act on that and never on this repository: a
target that changes with every commit makes two records incomparable, which is
the one thing a benchmark may not do.

Records are JSONL under `benchmarks/results/`, tagged with the harness commit,
the model, the quantization, the profile and the host - so a regression has a
date and a commit to bisect between.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
import tomllib
from collections.abc import Mapping, Sequence
from contextlib import AsyncExitStack
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Protocol

import typer

from niharness.adapters.git import GitVcs
from niharness.adapters.jsonl_events import JsonlEventSink
from niharness.adapters.mcp import connect_tools
from niharness.application.implement import ARTIFACT_SCHEMAS
from niharness.application.step_runner import StepRunner
from niharness.application.window import Window
from niharness.cli.blocking import blocking
from niharness.cli.output import emit, fail
from niharness.cli.retrieval import open_retrieval
from niharness.cli.unavailable import EX_UNAVAILABLE
from niharness.cli.wiring import backend_for, server_parameters
from niharness.config import HarnessConfig, ModelConfig
from niharness.domain.bench import (
    CaseScore,
    L1Task,
    L2Case,
    L2Score,
    SuiteScore,
    TaskScore,
    score_case,
    score_reviews,
    score_suite,
    score_task,
)
from niharness.domain.budgets import Budgets, fit_to_host
from niharness.domain.events import Payload
from niharness.domain.identity import RunId, StepId
from niharness.domain.messages import ResponseFormat
from niharness.domain.roles import DEFAULT_ROLES, Role
from niharness.domain.tool_loop import ArtifactKind, LoopConfig
from niharness.ports.vcs import Worktree
from niharness.prompts import assemble

SUITES = Path("benchmarks/suites")
RESULTS = Path("benchmarks/results")

LITTER = ("__pycache__", ".ruff_cache", ".pytest_cache", ".mypy_cache")
"""Tool caches, never copied into a task's fixture.

Running `ruff` or `pytest` in the suite's own fixture directory - which is
what checking its baseline invariant looks like - leaves a cache behind. The
fixture is then committed with that cache in it, so the tree hash the
state-cycle detector compares depends on which version of ruff somebody last
ran by hand, and two machines prepare measurably different fixtures from the
same checked-in files.
"""

TASK_TEMPLATE = """## Benchmark task `{id}`

{prompt}

Answer when you are done. This is one task on its own: there is no plan to
continue and nothing else to do afterwards.
"""


class SuiteError(RuntimeError):
    """Raised when a suite cannot be read."""


def load_l1(root: Path) -> tuple[tuple[L1Task, ...], tuple[str, ...]]:
    """Return the L1 tasks and the metric names the suite declares.

    Args:
        root: the repository root; the suite is read from `benchmarks/suites`.

    Returns:
        The tasks in file order, and the metrics the suite asks to be recorded.

    Raises:
        SuiteError: when the file is missing or does not parse into tasks.
    """
    path = root / SUITES / "l1" / "tasks.toml"
    if not path.is_file():
        msg = f"no L1 suite at {path}"
        raise SuiteError(msg)
    try:
        body = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as error:
        msg = f"{path}: {error}"
        raise SuiteError(msg) from error
    raw = body.get("task")
    if not isinstance(raw, list) or not raw:
        msg = f"{path} declares no [[task]]"
        raise SuiteError(msg)
    try:
        tasks = tuple(L1Task.model_validate(entry) for entry in raw)
    except ValueError as error:
        msg = f"{path}: {error}"
        raise SuiteError(msg) from error
    metrics = body.get("metrics", {}).get("record", [])
    return tasks, tuple(str(name) for name in metrics)


def prepare_fixture(root: Path, into: Path, *, suite: str = "l1") -> Worktree:
    """Copy a suite's fixture into `into` and make it a git repository.

    A repository rather than a bare directory because the tools expect one:
    the gate commits, `apply_patch` reverses a diff to tell "already applied"
    from "does not apply", and the state-cycle detector hashes the tree.

    Raises:
        SuiteError: when the fixture is missing or git refuses it.
    """
    fixture = root / SUITES / suite / "fixture"
    if not fixture.is_dir():
        msg = f"no {suite.upper()} fixture at {fixture}"
        raise SuiteError(msg)
    git = _git()
    shutil.copytree(fixture, into, dirs_exist_ok=True, ignore=shutil.ignore_patterns(*LITTER))
    try:
        for args in (
            ("init", "-q", "-b", "bench", "."),
            ("config", "user.email", "bench@localhost"),
            ("config", "user.name", "nih bench"),
            ("add", "-A"),
            ("commit", "-qm", "chore: the benchmark fixture"),
        ):
            _run(git, args, into)
        head = _run(git, ("rev-parse", "HEAD"), into)
    except (OSError, subprocess.CalledProcessError) as error:
        msg = f"cannot prepare the fixture repository in {into}: {error}"
        raise SuiteError(msg) from error
    return Worktree(path=str(into), branch="bench", base_sha=head.strip())


def _git() -> str:
    """Return git's path.

    Raises:
        SuiteError: when git is not installed. The fixture has to be a
            repository, so this is not something to work around.
    """
    found = shutil.which("git")
    if found is None:
        msg = "git is not installed; the benchmark fixture has to be a repository"
        raise SuiteError(msg)
    return found


def _run(binary: str, args: Sequence[str], cwd: Path) -> str:
    """Run a local binary with a fixed argv and return its output."""
    return subprocess.run(  # noqa: S603 - fixed argv, resolved binary, no shell
        [binary, *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


async def payloads_of(sink: JsonlEventSink, run_id: RunId) -> tuple[Payload, ...]:
    """Return one run's event payloads, in sequence order."""
    return tuple([event.body async for event in sink.replay(run_id)])


async def run_task(
    config: HarnessConfig,
    task: L1Task,
    *,
    model: ModelConfig,
    backend: object,
    scratch: Path,
    local_tools: bool,
) -> TaskScore:
    """Run one L1 task in its own fixture repository and score it.

    Raises:
        SuiteError: when the fixture cannot be prepared.
    """
    worktree = prepare_fixture(config.project_root, scratch / "repo")
    run_id = RunId(f"bench-{task.id}")
    sink = JsonlEventSink(scratch / "runs", run_id)
    vcs = GitVcs(Path(worktree.path))
    spec = DEFAULT_ROLES[Role.IMPLEMENTER]

    async with AsyncExitStack() as stack:
        retrieval = await stack.enter_async_context(open_retrieval(config))
        tools = await stack.enter_async_context(
            connect_tools(
                server_parameters(config, worktree, local=local_tools),
                (),
                limits=config.tools,
                local=retrieval.tools(config),
            ),
        )
        runner = StepRunner(
            backend=backend,  # type: ignore[arg-type]
            tools=tools,
            events=sink,
            vcs=vcs,
            worktree=worktree,
        )
        window = Window(backend, config.compact)  # type: ignore[arg-type]
        await window.add_prompt(assemble(spec.prompt_layers))
        await window.add_task(TASK_TEMPLATE.format(id=task.id, prompt=task.prompt.strip()))
        await runner.run(
            LoopConfig(
                stage="bench",
                role=Role.IMPLEMENTER,
                model=model.name,
                allowlist=spec.allowlist,
                budgets=fit_to_host(
                    Budgets(max_tool_calls=task.max_tool_calls),
                    model.measured_generation_tps,
                ),
                thresholds=config.loop,
                limits=config.tools,
                task_id=task.id,
                temperature=spec.temperature,
                # The same artifact a real step demands, because L1 measures
                # the model under the conditions it will actually face - and
                # "terminated the loop" means producing that artifact, not
                # merely stopping.
                final_artifact=ArtifactKind.FINAL_ANSWER,
                response_format=ResponseFormat(
                    name=ArtifactKind.FINAL_ANSWER.value,
                    json_schema=ARTIFACT_SCHEMAS[ArtifactKind.FINAL_ANSWER](),
                ),
            ),
            window,
            step_id=StepId(task.id),
        )
    return score_task(
        task,
        await payloads_of(sink, run_id),
        read_back(Path(worktree.path), {**task.expect_content, **task.forbid_content}),
    )


def read_back(root: Path, wanted: Mapping[str, tuple[str, ...]]) -> dict[str, str]:
    """Return the content of each path the task declares an end state for.

    Only those paths: reading the worktree wholesale would make a record
    depend on what else happens to be in it.
    """
    found: dict[str, str] = {}
    for path in wanted:
        target = root / path
        if target.is_file():
            found[path] = target.read_text(encoding="utf-8", errors="replace")
    return found


CASE_TEMPLATE = """## Review `{id}`

What this change claims to do:

**{title}** - {intent}

Review the commit below. This is one review on its own: there is nothing to
fix afterwards and no next task.
"""


def load_l2(root: Path) -> tuple[tuple[L2Case, ...], tuple[str, ...]]:
    """Return the L2 golden cases and the metric names the suite declares.

    Raises:
        SuiteError: when the file is missing or does not parse into cases.
    """
    path = root / SUITES / "l2" / "golden.toml"
    if not path.is_file():
        msg = f"no L2 suite at {path}"
        raise SuiteError(msg)
    try:
        body = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as error:
        msg = f"{path}: {error}"
        raise SuiteError(msg) from error
    raw = body.get("case")
    if not isinstance(raw, list) or not raw:
        msg = f"{path} declares no [[case]]"
        raise SuiteError(msg)
    try:
        cases = tuple(L2Case.model_validate(entry) for entry in raw)
    except ValueError as error:
        msg = f"{path}: {error}"
        raise SuiteError(msg) from error
    suite_cases = root / SUITES / "l2" / "cases"
    missing = [case.id for case in cases if not (suite_cases / case.id).is_dir()]
    if missing:
        msg = f"{path}: no files under cases/ for {', '.join(missing)}"
        raise SuiteError(msg)
    metrics = body.get("metrics", {}).get("record", [])
    return cases, tuple(str(name) for name in metrics)


def stage_case(root: Path, case: L2Case, into: Path) -> tuple[Worktree, int]:
    """Build the repository a review starts from, and say how big the change is.

    Two commits: the fixture, then the case's replacement files as one change.
    The reviewer therefore reads a diff git produced from two trees, which is
    both what a real review gets and the reason a case cannot stop applying.

    Returns:
        The worktree, and the change's size in added plus removed lines.

    Raises:
        SuiteError: when the case has no files or git refuses the commit.
    """
    prepare_fixture(root, into, suite="l2")
    source = root / SUITES / "l2" / "cases" / case.id
    if not source.is_dir():
        msg = f"no files for case {case.id} at {source}"
        raise SuiteError(msg)
    git = _git()
    shutil.copytree(source, into, dirs_exist_ok=True, ignore=shutil.ignore_patterns(*LITTER))
    message = f"{case.commit_type}: {case.title}\n\n{case.intent}"
    try:
        _run(git, ("add", "-A"), into)
        _run(git, ("commit", "-qm", message), into)
        head = _run(git, ("rev-parse", "HEAD"), into).strip()
        stat = _run(git, ("show", "--numstat", "--format=", "HEAD"), into)
    except (OSError, subprocess.CalledProcessError) as error:
        msg = f"cannot stage case {case.id} in {into}: {error}"
        raise SuiteError(msg) from error
    return Worktree(path=str(into), branch="bench", base_sha=head), _changed_lines(stat)


def _changed_lines(numstat: str) -> int:
    """Return added plus removed lines from `git show --numstat` output."""
    total = 0
    for line in numstat.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        for count in parts[:2]:
            if count.isdigit():
                total += int(count)
    return total


async def run_case(
    config: HarnessConfig,
    case: L2Case,
    *,
    model: ModelConfig,
    backend: object,
    scratch: Path,
    local_tools: bool,
) -> CaseScore:
    """Review one golden case and score the verdict against the human's.

    Raises:
        SuiteError: when the case cannot be staged.
    """
    worktree, diff_lines = stage_case(config.project_root, case, scratch / "repo")
    run_id = RunId(f"bench-{case.id}")
    sink = JsonlEventSink(scratch / "runs", run_id)
    vcs = GitVcs(Path(worktree.path))
    spec = DEFAULT_ROLES[Role.REVIEWER]
    diff = await vcs.diff(worktree)

    async with AsyncExitStack() as stack:
        retrieval = await stack.enter_async_context(open_retrieval(config))
        tools = await stack.enter_async_context(
            connect_tools(
                server_parameters(config, worktree, local=local_tools),
                (),
                limits=config.tools,
                local=retrieval.tools(config),
            ),
        )
        runner = StepRunner(
            backend=backend,  # type: ignore[arg-type]
            tools=tools,
            events=sink,
            vcs=vcs,
            worktree=worktree,
        )
        window = Window(backend, config.compact)  # type: ignore[arg-type]
        await window.add_prompt(assemble(spec.prompt_layers))
        await window.add_task(
            CASE_TEMPLATE.format(id=case.id, title=case.title, intent=case.intent),
        )
        await window.add_notes(f"The commit under review:\n\n```diff\n{diff.strip()}\n```")
        state = await runner.run(
            LoopConfig(
                stage="bench",
                role=Role.REVIEWER,
                model=model.name,
                allowlist=spec.allowlist,
                budgets=fit_to_host(
                    Budgets(max_tool_calls=case.max_tool_calls),
                    model.measured_generation_tps,
                ),
                thresholds=config.loop,
                limits=config.tools,
                task_id=case.id,
                temperature=spec.temperature,
                final_artifact=ArtifactKind.VERDICT,
            ),
            window,
            step_id=StepId(case.id),
        )
    return score_case(
        case,
        state.verdict,
        await payloads_of(sink, run_id),
        diff_lines=diff_lines,
    )


def record(
    config: HarnessConfig,
    score: SuiteScore,
    *,
    model: ModelConfig,
    metrics: Sequence[str],
    seconds: float,
    commit: str,
    dirty: bool,
    attempt: int = 1,
    of: int = 1,
) -> dict[str, object]:
    """Return the JSONL record for one suite run."""
    declared = {name: score.metrics()[name] for name in metrics if name in score.metrics()}
    return {
        "layer": score.layer,
        "date": datetime.now(tz=UTC).date().isoformat(),
        "harness_commit": commit,
        "harness_dirty": dirty,
        "model": model.name,
        "quantization": model.quantization,
        "profile": config.profile,
        "base_url": model.base_url,
        "tasks": len(score.tasks),
        "pass": attempt,
        "of_passes": of,
        **declared,
        "passed_ratio": score.passed_ratio,
        "violations": score.violations(),
        "violation_examples": score.examples(),
        "wall_seconds": round(seconds, 1),
        "per_task": [
            {
                "id": task.task_id,
                "passed": task.passed,
                "calls": task.calls,
                "rejected": task.rejected,
                "interventions": task.interventions,
                "violations": task.violations,
                "stop_reason": task.stop_reason.value if task.stop_reason else None,
                "notes": list(task.notes),
            }
            for task in score.tasks
        ],
    }


def record_reviews(
    config: HarnessConfig,
    score: L2Score,
    *,
    model: ModelConfig,
    metrics: Sequence[str],
    seconds: float,
    commit: str,
    dirty: bool,
    attempt: int = 1,
    of: int = 1,
) -> dict[str, object]:
    """Return the JSONL record for one review suite run."""
    declared = {name: score.metrics()[name] for name in metrics if name in score.metrics()}
    return {
        "layer": score.layer,
        "date": datetime.now(tz=UTC).date().isoformat(),
        "harness_commit": commit,
        "harness_dirty": dirty,
        "model": model.name,
        "quantization": model.quantization,
        "profile": config.profile,
        "base_url": model.base_url,
        "cases": len(score.cases),
        "pass": attempt,
        "of_passes": of,
        **declared,
        "passed_ratio": score.passed_ratio,
        "confusion": {
            "true_positives": score.true_positives,
            "false_positives": score.false_positives,
            "false_negatives": score.false_negatives,
            "undecided": score.undecided,
        },
        "wall_seconds": round(seconds, 1),
        "per_case": [
            {
                "id": case.case_id,
                "expected": case.expected.value,
                "decided": case.decided.value if case.decided else None,
                "passed": case.passed,
                "located": case.located,
                "reasoned": case.reasoned,
                "evidence_ok": case.evidence_ok,
                "calls": case.calls,
                "interventions": case.interventions,
                "diff_lines": case.diff_lines,
                "notes": list(case.notes),
            }
            for case in score.cases
        ],
    }


def human_reviews(entry: dict[str, object], score: L2Score) -> str:
    """Render a review suite run: the metrics, the matrix, then what went wrong."""
    lines = [
        f"L2 on {entry['model']} ({entry['quantization']}), {entry['cases']} case(s) "
        f"in {entry['wall_seconds']}s"
        + (
            " [uncommitted tree: a working note, not a result]"
            if entry["harness_dirty"]
            else ""
        ),
        "",
    ]
    for name, value in score.metrics().items():
        lines.append(f"  {name:<28} {value}")
    lines += [
        "",
        f"  caught {score.true_positives} defect(s), missed {score.false_negatives}, "
        f"refused {score.false_positives} correct change(s), "
        f"{score.undecided} case(s) produced no verdict",
        "",
    ]
    for case in score.cases:
        mark = "ok  " if case.passed else "FAIL"
        said = case.decided.value if case.decided else "-"
        lines.append(
            f"  [{mark}] {case.case_id:<34} wanted {case.expected.value}, said {said}, "
            f"{case.calls} call(s)"
        )
        lines += [f"         {note}" for note in case.notes]
    return "\n".join(lines)


def _dirty(root: Path) -> bool:
    """Whether the *code* had uncommitted changes when the run started.

    Without it two records can carry the same commit and not be comparable:
    a measurement taken either side of an uncommitted change says the same
    `harness_commit` for both, which is the one thing a record must not do. A
    record from a dirty tree is a working note, not a result.

    Measured once, before the first pass, and with the results directory
    excluded. Writing a record makes the tree dirty, so asking per pass made
    every pass after the first report `true` - the flag answering a question
    about its own output rather than about the code.
    """
    try:
        changed = _run(_git(), ("status", "--porcelain", "--", ":!" + str(RESULTS)), root)
    except (OSError, subprocess.CalledProcessError, SuiteError):
        return True
    return bool(changed.strip())


def _commit(root: Path) -> str:
    """Return the harness's short commit, or 'unknown' outside a repository.

    Read once per run by the caller, never per record: the code under test was
    loaded when the process started, so a record written after a commit would
    otherwise name a commit it never ran.
    """
    try:
        return _run(_git(), ("rev-parse", "--short", "HEAD"), root).strip() or "unknown"
    except (OSError, subprocess.CalledProcessError, SuiteError):
        return "unknown"


def write_record(root: Path, entry: dict[str, object]) -> Path:
    """Append one record to this layer's JSONL file and return its path."""
    target = root / RESULTS / f"{entry['layer']}-{entry['model']}-{entry['date']}.jsonl".lower()
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        handle.write(f"{json.dumps(entry, sort_keys=True)}\n")
    return target


def human(entry: dict[str, object], score: SuiteScore) -> str:
    """Render a suite run for an operator: the table, then what failed."""
    lines = [
        f"L1 on {entry['model']} ({entry['quantization']}), {entry['tasks']} task(s) "
        f"in {entry['wall_seconds']}s"
        + (
            " [uncommitted tree: a working note, not a result]"
            if entry["harness_dirty"]
            else ""
        ),
        "",
    ]
    for name, value in score.metrics().items():
        lines.append(f"  {name:<26} {value}")
    refused = score.violations()
    if refused:
        detail = ", ".join(f"{rule} x{count}" for rule, count in refused.items())
        lines.append(f"  {'refused by':<26} {detail}")
        for rule, example in score.examples().items():
            lines.append(f"    {rule}: {example}")
    lines.append("")
    for task in score.tasks:
        mark = "ok  " if task.passed else "FAIL"
        lines.append(
            f"  [{mark}] {task.task_id:<30} {task.calls} call(s), "
            f"{task.rejected} refused, {task.interventions} intervention(s)"
        )
        lines += [f"         {note}" for note in task.notes]
    return "\n".join(lines)


async def _scored(
    config: HarnessConfig,
    unit: L1Task | L2Case,
    *,
    model: ModelConfig,
    backend: object,
    scratch: Path,
    local_tools: bool,
) -> TaskScore | CaseScore:
    """Run one unit of whichever suite it belongs to."""
    if isinstance(unit, L1Task):
        return await run_task(
            config,
            unit,
            model=model,
            backend=backend,
            scratch=scratch,
            local_tools=local_tools,
        )
    return await run_case(
        config,
        unit,
        model=model,
        backend=backend,
        scratch=scratch,
        local_tools=local_tools,
    )


class LayerScore(Protocol):
    """What the across-passes report needs from any layer's score.

    Structural rather than a base class: the two layers' scores have nothing
    in common except that both can report metrics and per-unit outcomes, and
    inventing a shared parent to express that would put L2's vocabulary in
    L1's type.
    """

    def metrics(self) -> dict[str, float]:
        """The metrics this layer reports, by name."""
        ...

    def outcomes(self) -> tuple[tuple[str, bool], ...]:
        """Each unit's id and whether it passed."""
        ...


async def _one_pass(
    config: HarnessConfig,
    units: Sequence[L1Task] | Sequence[L2Case],
    *,
    layer: str,
    attempt: int,
    repeat: int,
    model: ModelConfig,
    backend: object,
    metrics: Sequence[str],
    commit: str,
    dirty: bool,
    local_tools: bool,
    json_mode: bool,
) -> tuple[dict[str, object], LayerScore]:
    """Run every unit of the suite once and build the record for that pass."""
    started = time.monotonic()
    scored: list[TaskScore | CaseScore] = []
    for item in units:
        with TemporaryDirectory(prefix=f"nih-bench-{item.id}-") as scratch:
            if not json_mode:
                typer.echo(f"  [{attempt}/{repeat}] {item.id} ...", err=True)
            scored.append(
                await _scored(
                    config,
                    item,
                    model=model,
                    backend=backend,
                    scratch=Path(scratch),
                    local_tools=local_tools,
                ),
            )
    seconds = time.monotonic() - started
    if layer == "L1":
        suite = score_suite([one for one in scored if isinstance(one, TaskScore)])
        return record(
            config,
            suite,
            model=model,
            metrics=metrics,
            seconds=seconds,
            commit=commit,
            dirty=dirty,
            attempt=attempt,
            of=repeat,
        ), suite
    reviews = score_reviews([one for one in scored if isinstance(one, CaseScore)])
    return record_reviews(
        config,
        reviews,
        model=model,
        metrics=metrics,
        seconds=seconds,
        commit=commit,
        dirty=dirty,
        attempt=attempt,
        of=repeat,
    ), reviews


def bench(
    config: HarnessConfig,
    *,
    layer: str,
    repeat: int,
    local_tools: bool,
    json_mode: bool,
) -> None:
    """Run a benchmark layer `repeat` times and append one record per pass.

    One pass cannot tell a change from the model's own variance. Two runs of
    the same 14 tasks differed by three tasks with nothing changed in between,
    and reading either as a result would have been wrong in both directions.
    So a pass is a record, several passes are several records in one file, and
    the spread is visible rather than averaged into silence.

    Raises:
        typer.Exit: when the layer has no runner, the suite cannot be read, or
            the model does not answer.
    """
    chosen = layer.upper()
    if chosen not in {"L1", "L2"}:
        fail(
            f"no runner for {chosen} yet; L1 and L2 are the ones that exist. "
            "See docs/benchmarks/methodology.md",
            json_mode=json_mode,
            code=EX_UNAVAILABLE,
        )
        raise typer.Exit(EX_UNAVAILABLE)

    model = config.models.main
    # Both read once, before the first pass. `harness_commit` was read as each
    # record was written, so a run that straddled a commit labelled its later
    # passes with code they were not running - the same shape of mistake the
    # dirty flag made, and just as quiet.
    commit = _commit(config.project_root)
    dirty = _dirty(config.project_root)
    backend = backend_for(model)
    passes: list[tuple[dict[str, object], LayerScore]] = []
    written: list[Path] = []
    unit = "task" if chosen == "L1" else "case"

    try:
        units, metrics = (
            load_l1(config.project_root) if chosen == "L1" else load_l2(config.project_root)
        )
    except SuiteError as error:
        fail(str(error), json_mode=json_mode)
        raise typer.Exit(1) from error

    async def drive() -> None:
        try:
            for attempt in range(1, max(1, repeat) + 1):
                entry, score = await _one_pass(
                    config,
                    units,
                    layer=chosen,
                    attempt=attempt,
                    repeat=max(1, repeat),
                    model=model,
                    backend=backend,
                    metrics=metrics,
                    commit=commit,
                    dirty=dirty,
                    local_tools=local_tools,
                    json_mode=json_mode,
                )
                # Written as the pass completes rather than at the end: three
                # passes are twelve minutes of model time, and an interrupted
                # run should keep what it already measured.
                written.append(write_record(config.project_root, entry))
                passes.append((entry, score))
        finally:
            await backend.aclose()

    broke = ""
    try:
        blocking(drive())
    except SuiteError as error:
        broke = str(error)
    except Exception as error:  # noqa: BLE001 - a pass may fail any number of ways
        # Twelve minutes of model time should not be lost because the last
        # pass died. What was measured is already on disk; this says what
        # stopped the rest, and the command still exits non-zero.
        broke = f"{type(error).__name__}: {error}"
    if broke and not passes:
        fail(broke, json_mode=json_mode)
        raise typer.Exit(1)

    target = written[-1] if written else config.project_root / RESULTS
    note = f"\n\nstopped after {len(passes)} pass(es): {broke}" if broke else ""
    emit(
        {
            "ok": not broke,
            "record": str(target),
            "passes": [entry for entry, _ in passes],
            **({"stopped": broke} if broke else {}),
        },
        json_mode=json_mode,
        human=f"{_rendered(passes, unit=unit)}\n\nrecorded in {target}{note}",
    )
    if broke:
        raise typer.Exit(1)


def _one_rendered(entry: dict[str, object], score: LayerScore) -> str:
    """Render one pass with the report its layer has."""
    if isinstance(score, SuiteScore):
        return human(entry, score)
    if isinstance(score, L2Score):
        return human_reviews(entry, score)
    msg = f"no report for {type(score).__name__}"  # pragma: no cover - both layers covered
    raise AssertionError(msg)  # pragma: no cover


def _rendered(
    passes: Sequence[tuple[dict[str, object], LayerScore]],
    *,
    unit: str,
) -> str:
    """Render every pass, and the spread when there is more than one."""
    parts = [_one_rendered(entry, score) for entry, score in passes]
    if len(passes) < 2:
        return "\n\n".join(parts)
    return "\n\n".join([*parts, _spread([score for _, score in passes], unit=unit)])


def _spread(scores: Sequence[LayerScore], *, unit: str) -> str:
    """Report each metric's range across passes, and which units were not stable.

    The point of repeating: a unit that passes once and fails once has said
    something different from one that passes every time, and a single number
    cannot carry the difference.
    """
    lines = [f"across {len(scores)} pass(es):"]
    names = scores[0].metrics()
    for name in names:
        values = [score.metrics()[name] for score in scores]
        lines.append(f"  {name:<28} {min(values)} - {max(values)}")
    outcomes: dict[str, list[bool]] = {}
    for score in scores:
        for identifier, passed in score.outcomes():
            outcomes.setdefault(identifier, []).append(passed)
    flaky = {name: runs for name, runs in outcomes.items() if len(set(runs)) > 1}
    always = sorted(name for name, runs in outcomes.items() if not any(runs))
    if flaky:
        lines.append("  not stable across passes:")
        lines += [
            f"    {name}: {sum(runs)}/{len(runs)} passed"
            for name, runs in sorted(flaky.items())
        ]
    if always:
        lines.append(f"  failed every pass: {', '.join(always)}")
    _ = unit
    return "\n".join(lines)
