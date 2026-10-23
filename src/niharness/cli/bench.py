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

import typer

from niharness.adapters.git import GitVcs
from niharness.adapters.jsonl_events import JsonlEventSink
from niharness.adapters.mcp import connect_tools
from niharness.application.implement import ARTIFACT_SCHEMAS
from niharness.application.step_runner import StepRunner
from niharness.application.window import Window
from niharness.cli.blocking import blocking
from niharness.cli.implement import backend_for, server_parameters
from niharness.cli.output import emit, fail
from niharness.cli.retrieval import open_retrieval
from niharness.cli.unavailable import EX_UNAVAILABLE
from niharness.config import HarnessConfig, ModelConfig
from niharness.domain.bench import L1Task, SuiteScore, TaskScore, score_suite, score_task
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


def prepare_fixture(root: Path, into: Path) -> Worktree:
    """Copy the suite's fixture into `into` and make it a git repository.

    A repository rather than a bare directory because the tools expect one:
    the gate commits, `apply_patch` reverses a diff to tell "already applied"
    from "does not apply", and the state-cycle detector hashes the tree.

    Raises:
        SuiteError: when the fixture is missing or git refuses it.
    """
    fixture = root / SUITES / "l1" / "fixture"
    if not fixture.is_dir():
        msg = f"no L1 fixture at {fixture}"
        raise SuiteError(msg)
    git = _git()
    shutil.copytree(fixture, into, dirs_exist_ok=True)
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
        read_back(Path(worktree.path), task.expect_content),
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


def record(
    config: HarnessConfig,
    score: SuiteScore,
    *,
    model: ModelConfig,
    metrics: Sequence[str],
    seconds: float,
    attempt: int = 1,
    of: int = 1,
) -> dict[str, object]:
    """Return the JSONL record for one suite run."""
    declared = {name: score.metrics()[name] for name in metrics if name in score.metrics()}
    return {
        "layer": score.layer,
        "date": datetime.now(tz=UTC).date().isoformat(),
        "harness_commit": _commit(config.project_root),
        "harness_dirty": _dirty(config.project_root),
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


def _dirty(root: Path) -> bool:
    """Whether the tree had uncommitted changes when this was measured.

    Without it two records can carry the same commit and not be comparable:
    a measurement taken before and after an uncommitted change says the same
    `harness_commit` for both, which is the one thing a record must not do.
    A record from a dirty tree is a working note, not a result.
    """
    try:
        return bool(_run(_git(), ("status", "--porcelain"), root).strip())
    except (OSError, subprocess.CalledProcessError, SuiteError):
        return True


def _commit(root: Path) -> str:
    """Return the harness's short commit, or 'unknown' outside a repository."""
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
    if layer.upper() != "L1":
        fail(
            f"no runner for {layer} yet; L1 is the one that exists. "
            "See docs/benchmarks/methodology.md",
            json_mode=json_mode,
            code=EX_UNAVAILABLE,
        )
        raise typer.Exit(EX_UNAVAILABLE)
    try:
        tasks, metrics = load_l1(config.project_root)
    except SuiteError as error:
        fail(str(error), json_mode=json_mode)
        raise typer.Exit(1) from error

    model = config.models.main
    backend = backend_for(model)
    passes: list[tuple[dict[str, object], SuiteScore]] = []
    written: list[Path] = []

    async def drive() -> None:
        try:
            for attempt in range(1, max(1, repeat) + 1):
                started = time.monotonic()
                scores: list[TaskScore] = []
                for task in tasks:
                    with TemporaryDirectory(prefix=f"nih-bench-{task.id}-") as scratch:
                        if not json_mode:
                            typer.echo(f"  [{attempt}/{repeat}] {task.id} ...", err=True)
                        scores.append(
                            await run_task(
                                config,
                                task,
                                model=model,
                                backend=backend,
                                scratch=Path(scratch),
                                local_tools=local_tools,
                            ),
                        )
                score = score_suite(scores)
                entry = record(
                    config,
                    score,
                    model=model,
                    metrics=metrics,
                    seconds=time.monotonic() - started,
                    attempt=attempt,
                    of=max(1, repeat),
                )
                # Written as the pass completes rather than at the end: three
                # passes are twelve minutes of model time, and an interrupted
                # run should keep what it already measured.
                written.append(write_record(config.project_root, entry))
                passes.append((entry, score))
        finally:
            await backend.aclose()

    try:
        blocking(drive())
    except SuiteError as error:
        fail(str(error), json_mode=json_mode)
        raise typer.Exit(1) from error

    target = written[-1] if written else config.project_root / RESULTS
    emit(
        {
            "ok": True,
            "record": str(target),
            "passes": [entry for entry, _ in passes],
        },
        json_mode=json_mode,
        human=f"{_rendered(passes)}\n\nrecorded in {target}",
    )


def _rendered(passes: Sequence[tuple[dict[str, object], SuiteScore]]) -> str:
    """Render every pass, and the spread when there is more than one."""
    parts = [human(entry, score) for entry, score in passes]
    if len(passes) < 2:
        return "\n\n".join(parts)
    return "\n\n".join([*parts, _spread([score for _, score in passes])])


def _spread(scores: Sequence[SuiteScore]) -> str:
    """Report each metric's range across passes, and which tasks were not stable.

    The point of repeating: a task that passes once and fails once has said
    something different from one that passes every time, and a single number
    cannot carry the difference.
    """
    lines = [f"across {len(scores)} pass(es):"]
    names = scores[0].metrics()
    for name in names:
        values = [score.metrics()[name] for score in scores]
        lines.append(f"  {name:<26} {min(values)} - {max(values)}")
    outcomes: dict[str, list[bool]] = {}
    for score in scores:
        for task in score.tasks:
            outcomes.setdefault(task.task_id, []).append(task.passed)
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
    return "\n".join(lines)
