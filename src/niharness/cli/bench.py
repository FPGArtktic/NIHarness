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
import sys
import time
import tomllib
import uuid
from collections.abc import Mapping, Sequence
from contextlib import AsyncExitStack
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Protocol

import typer
from typer.core import TyperGroup

from niharness.adapters.git import GitVcs
from niharness.adapters.jsonl_events import JsonlEventSink
from niharness.adapters.mcp import connect_tools
from niharness.application.implement import ARTIFACT_SCHEMAS, ImplementationRun, RunResult
from niharness.application.step_runner import StepRunner
from niharness.application.summarizer import ModelSummarizer
from niharness.application.window import Window
from niharness.cli.blocking import blocking
from niharness.cli.init import probe as probe_hardware
from niharness.cli.output import emit, fail
from niharness.cli.retrieval import open_retrieval
from niharness.cli.unavailable import EX_UNAVAILABLE
from niharness.cli.wiring import backend_for, server_parameters
from niharness.config import HarnessConfig, ModelConfig
from niharness.domain.artifacts import Plan, Slug, Task
from niharness.domain.bench import (
    BuildScore,
    CaseScore,
    L0Probe,
    L0Score,
    L1Task,
    L2Case,
    L2Score,
    L3Score,
    L3Task,
    ReportRow,
    SuiteScore,
    TaskScore,
    Throughput,
    report_markdown,
    score_build,
    score_builds,
    score_case,
    score_reviews,
    score_suite,
    score_task,
    score_throughput,
    throughput,
)
from niharness.domain.budgets import Budgets, fit_to_host
from niharness.domain.events import Payload
from niharness.domain.identity import RunId, StepId, digest_json
from niharness.domain.messages import ChatRequest, Message, MessageRole, ResponseFormat
from niharness.domain.roles import DEFAULT_ROLES, Role
from niharness.domain.tool_loop import ArtifactKind, LoopConfig
from niharness.ports.model_backend import ModelBackend
from niharness.ports.tokenizer import Tokenizer
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


FILLER = (
    "The sampler reads a run of values, keeps the ones inside the band, and "
    "reports their mean. Nothing here is executed; this text exists only to "
    "occupy a measured number of tokens. "
)
"""Prose for an L0 prompt.

Prose rather than a repeated token, because a tokenizer compresses repetition
and the prompt would then be shorter in tokens than in characters by a factor
nobody can predict. Trimmed to the measured count either way.
"""


def load_l0(root: Path) -> tuple[L0Probe, ...]:
    """Return the L0 probes the suite declares.

    Raises:
        SuiteError: when the file is missing or does not parse into probes.
    """
    path = root / SUITES / "l0" / "probes.toml"
    if not path.is_file():
        msg = f"no L0 suite at {path}"
        raise SuiteError(msg)
    try:
        body = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as error:
        msg = f"{path}: {error}"
        raise SuiteError(msg) from error
    raw = body.get("probe")
    if not isinstance(raw, list) or not raw:
        msg = f"{path} declares no [[probe]]"
        raise SuiteError(msg)
    try:
        return tuple(L0Probe.model_validate(entry) for entry in raw)
    except ValueError as error:
        msg = f"{path}: {error}"
        raise SuiteError(msg) from error


async def prompt_of(tokenizer: Tokenizer, tokens: int) -> tuple[str, int]:
    """Return filler text of about `tokens` tokens, and what it actually counted.

    Counted through the backend's own tokenizer rather than estimated from
    characters, for the same reason the window is: a character heuristic is
    wrong by a different amount for every model, and a throughput number
    divided by a wrong token count is a wrong throughput number.

    Returns:
        The text and its measured token count.
    """
    per_block = max(1, await tokenizer.count_text(FILLER))
    text = FILLER * max(1, tokens // per_block)
    counted = await tokenizer.count_text(text)
    # Two corrections are enough to land inside a percent: the first scales by
    # the error, the second fixes the rounding. Looping to exactness would cost
    # a round trip per token for no measurable gain.
    for _ in range(2):
        if counted == 0 or abs(counted - tokens) <= max(8, tokens // 100):
            break
        blocks = max(1, round(len(text) / len(FILLER) * tokens / counted))
        text = FILLER * blocks
        counted = await tokenizer.count_text(text)
    return text, counted


async def run_probe(
    probe: L0Probe,
    *,
    model: ModelConfig,
    backend: ModelBackend,
    tokenizer: Tokenizer,
) -> Throughput:
    """Time three requests at this probe's prompt length and derive both rates.

    The prompt carries a nonce unique to this call, which is what makes the
    first request cold. Without it the prompt repeats between runs and the
    server's prefix cache answers it: measured, that produced a prompt rate of
    68,000 tok/s on a laptop GPU. The nonce goes in the **first** line,
    because a prefix cache matches a prefix.

    The cache is then used deliberately for the other two requests - see
    :func:`niharness.domain.bench.throughput`.
    """
    text, _ = await prompt_of(tokenizer, probe.context_tokens)
    nonce = uuid.uuid4().hex
    content = f"// probe {nonce}\n{text}"
    counted = await tokenizer.count_text(content)

    async def timed(limit: int) -> tuple[float, int]:
        """Send one request and return how long it took and what it produced."""
        started = time.monotonic()
        response = await backend.chat(
            ChatRequest(
                model=model.name,
                messages=(Message(role=MessageRole.USER, content=content),),
                max_tokens=limit,
                temperature=0.0,
                seed=0,
            ),
        )
        return time.monotonic() - started, response.usage.completion_tokens

    cold_seconds, _ = await timed(1)
    warm_seconds, _ = await timed(1)
    long_seconds, generated = await timed(probe.generate_tokens)
    return throughput(
        probe,
        prompt_tokens=counted,
        generated=generated,
        cold_seconds=cold_seconds,
        warm_seconds=warm_seconds,
        long_seconds=long_seconds,
    )


def record_throughput(
    config: HarnessConfig,
    score: L0Score,
    *,
    model: ModelConfig,
    seconds: float,
    commit: str,
    dirty: bool,
    skipped: Sequence[str] = (),
    attempt: int = 1,
    of: int = 1,
) -> dict[str, object]:
    """Return the JSONL record for one throughput run.

    Every metric is recorded rather than a declared subset: the names are
    derived from the probes, so a suite that declares them separately would
    only be a second place for them to disagree.
    """
    host = probe_hardware()
    return {
        "layer": score.layer,
        "date": datetime.now(tz=UTC).date().isoformat(),
        "harness_commit": commit,
        "harness_dirty": dirty,
        "model": model.name,
        "quantization": model.quantization,
        "profile": config.profile,
        "base_url": model.base_url,
        "context_window": model.context_window,
        "pass": attempt,
        "of_passes": of,
        **score.metrics(),
        "host": host.model_dump(mode="json"),
        "skipped": list(skipped),
        "wall_seconds": round(seconds, 1),
        "per_probe": [sample.model_dump(mode="json") for sample in score.samples],
    }


def human_throughput(entry: dict[str, object], score: L0Score) -> str:
    """Render a throughput run: a row per context size, and what was skipped."""
    host = entry["host"] if isinstance(entry["host"], dict) else {}
    lines = [
        f"L0 on {entry['model']} ({entry['quantization']}) at {entry['base_url']}, "
        f"{entry['wall_seconds']}s"
        + (
            " [uncommitted tree: a working note, not a result]"
            if entry["harness_dirty"]
            else ""
        ),
        f"  host: {host.get('cpu_cores')} core(s), {host.get('ram_mb')} MiB RAM"
        + (
            f", {host.get('gpu_name')} {host.get('vram_mb')} MiB"
            if host.get("gpu_name")
            else ""
        ),
        "",
        f"  {'context':<12}{'prompt tok/s':>14}{'generation tok/s':>18}",
    ]
    for sample in score.samples:
        lines.append(
            f"  {sample.probe_id:<12}{sample.prompt_tps:>14}{sample.generation_tps:>18}"
        )
        lines += [f"      {note}" for note in sample.notes]
    skipped = entry["skipped"]
    if isinstance(skipped, list) and skipped:
        lines += ["", f"  skipped: {', '.join(str(one) for one in skipped)}"]
    return "\n".join(lines)


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
        "pathologies": score.pathologies(),
        "tools": score.tools(),
        "inference_seconds": score.inference_seconds,
        "tool_seconds": score.tool_seconds,
        "overhead_seconds": round(
            max(0.0, seconds - score.inference_seconds - score.tool_seconds), 1
        ),
        "wall_seconds": round(seconds, 1),
        "per_task": [
            {
                "id": task.task_id,
                "passed": task.passed,
                "calls": task.calls,
                "rejected": task.rejected,
                "interventions": task.interventions,
                "pathologies": task.pathologies,
                "tools": task.tools,
                "inference_seconds": task.inference_seconds,
                "tool_seconds": task.tool_seconds,
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
        "pathologies": score.pathologies(),
        "tools": score.tools(),
        "inference_seconds": score.inference_seconds,
        "tool_seconds": score.tool_seconds,
        "overhead_seconds": round(
            max(0.0, seconds - score.inference_seconds - score.tool_seconds), 1
        ),
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
                "pathologies": case.pathologies,
                "tools": case.tools,
                "inference_seconds": case.inference_seconds,
                "tool_seconds": case.tool_seconds,
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
    fired = score.pathologies()
    if fired:
        detail = ", ".join(f"{name} x{count}" for name, count in fired.items())
        lines.append(f"  {'intervened on':<28} {detail}")
    used = score.tools()
    if used:
        detail = ", ".join(f"{name} x{count}" for name, count in used.items())
        lines.append(f"  {'called':<28} {detail}")
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
    fired = score.pathologies()
    if fired:
        detail = ", ".join(f"{name} x{count}" for name, count in fired.items())
        lines.append(f"  {'intervened on':<26} {detail}")
    used = score.tools()
    if used:
        detail = ", ".join(f"{name} x{count}" for name, count in used.items())
        lines.append(f"  {'called':<26} {detail}")
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
    unit: BenchUnit,
    *,
    model: ModelConfig,
    backend: object,
    utility: object,
    scratch: Path,
    local_tools: bool,
) -> TaskScore | CaseScore | BuildScore:
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
    if isinstance(unit, L3Task):
        return await run_build(
            config,
            unit,
            model=model,
            backend=backend,
            utility=utility,
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


BenchUnit = L1Task | L2Case | L3Task
"""A unit of a graded suite: a tool-calling task, a review case, a whole task."""


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


def _suite(root: Path, layer: str) -> tuple[Sequence[BenchUnit], tuple[str, ...]]:
    """Return the graded layer's units and declared metrics, empty for L0.

    Raises:
        SuiteError: when the suite cannot be read.
    """
    if layer == "L1":
        return load_l1(root)
    if layer == "L2":
        return load_l2(root)
    if layer == "L3":
        return load_l3(root)
    return (), ()


async def _throughput_pass(
    config: HarnessConfig,
    probes: Sequence[L0Probe],
    *,
    attempt: int,
    repeat: int,
    model: ModelConfig,
    backend: ModelBackend,
    tokenizer: Tokenizer,
    commit: str,
    dirty: bool,
    json_mode: bool,
) -> tuple[dict[str, object], LayerScore]:
    """Measure every probe once and build the record for that pass.

    A probe that does not leave room for its own generation inside the served
    context window is skipped rather than sent: the backend would refuse it,
    and a refusal recorded as a zero would read as "this host is infinitely
    slow at 64k". Prompt *plus* generation, because a 64k prompt in a 64k
    window has nowhere to put the answer - which is the one case a naive
    comparison lets through.
    """
    started = time.monotonic()
    samples: list[Throughput] = []
    skipped: list[str] = []
    for probe in probes:
        needed = probe.context_tokens + probe.generate_tokens
        if needed > model.context_window:
            skipped.append(
                f"{probe.id} (needs {needed} tokens with its generation; "
                f"the served window is {model.context_window})"
            )
            continue
        if not json_mode:
            typer.echo(f"  [{attempt}/{repeat}] {probe.id} ...", err=True)
        samples.append(
            await run_probe(probe, model=model, backend=backend, tokenizer=tokenizer),
        )
    score = score_throughput(samples)
    return record_throughput(
        config,
        score,
        model=model,
        seconds=time.monotonic() - started,
        commit=commit,
        dirty=dirty,
        skipped=skipped,
        attempt=attempt,
        of=repeat,
    ), score


async def _one_pass(
    config: HarnessConfig,
    units: Sequence[BenchUnit],
    *,
    layer: str,
    attempt: int,
    repeat: int,
    model: ModelConfig,
    backend: object,
    utility: object,
    metrics: Sequence[str],
    commit: str,
    dirty: bool,
    local_tools: bool,
    json_mode: bool,
) -> tuple[dict[str, object], LayerScore]:
    """Run every unit of the suite once and build the record for that pass."""
    started = time.monotonic()
    scored: list[TaskScore | CaseScore | BuildScore] = []
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
                    utility=utility,
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
    if layer == "L3":
        builds = score_builds([one for one in scored if isinstance(one, BuildScore)])
        return record_builds(
            config,
            builds,
            model=model,
            metrics=metrics,
            seconds=seconds,
            commit=commit,
            dirty=dirty,
            attempt=attempt,
            of=repeat,
        ), builds
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
    if chosen not in {"L0", "L1", "L2", "L3"}:
        fail(
            f"no such layer as {chosen}; L0, L1, L2 and L3 are the ones that exist. "
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
    # L3 runs the whole pipeline, which compacts its window with the utility
    # model. Opened only for that layer: a client to a service L1 never calls
    # would be one more way for a pass to fail before it measured anything.
    utility = backend_for(config.models.utility) if chosen == "L3" else None
    passes: list[tuple[dict[str, object], LayerScore]] = []
    written: list[Path] = []

    try:
        probes = load_l0(config.project_root) if chosen == "L0" else ()
        units, metrics = _suite(config.project_root, chosen)
    except SuiteError as error:
        fail(str(error), json_mode=json_mode)
        raise typer.Exit(1) from error

    async def drive() -> None:
        try:
            for attempt in range(1, max(1, repeat) + 1):
                entry, score = await (
                    _throughput_pass(
                        config,
                        probes,
                        attempt=attempt,
                        repeat=max(1, repeat),
                        model=model,
                        backend=backend,
                        tokenizer=backend,
                        commit=commit,
                        dirty=dirty,
                        json_mode=json_mode,
                    )
                    if chosen == "L0"
                    else _one_pass(
                        config,
                        units,
                        layer=chosen,
                        attempt=attempt,
                        repeat=max(1, repeat),
                        model=model,
                        backend=backend,
                        utility=utility,
                        metrics=metrics,
                        commit=commit,
                        dirty=dirty,
                        local_tools=local_tools,
                        json_mode=json_mode,
                    )
                )
                # Written as the pass completes rather than at the end: three
                # passes are twelve minutes of model time, and an interrupted
                # run should keep what it already measured.
                written.append(write_record(config.project_root, entry))
                passes.append((entry, score))
        finally:
            await backend.aclose()
            if utility is not None:
                await utility.aclose()

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
        human=f"{_rendered(passes)}\n\nrecorded in {target}{note}",
    )
    if broke:
        raise typer.Exit(1)


def _one_rendered(entry: dict[str, object], score: LayerScore) -> str:
    """Render one pass with the report its layer has."""
    if isinstance(score, SuiteScore):
        return human(entry, score)
    if isinstance(score, L2Score):
        return human_reviews(entry, score)
    if isinstance(score, L3Score):
        return human_builds(entry, score)
    if isinstance(score, L0Score):
        return human_throughput(entry, score)
    msg = f"no report for {type(score).__name__}"  # pragma: no cover - every layer covered
    raise AssertionError(msg)  # pragma: no cover


def _rendered(passes: Sequence[tuple[dict[str, object], LayerScore]]) -> str:
    """Render every pass, and the spread when there is more than one."""
    parts = [_one_rendered(entry, score) for entry, score in passes]
    if len(passes) < 2:
        return "\n\n".join(parts)
    return "\n\n".join([*parts, _spread([score for _, score in passes])])


def _spread(scores: Sequence[LayerScore]) -> str:
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
    return "\n".join(lines)


# ----------------------------------------------------------------- the report

RECORD_METADATA = frozenset(
    {"pass", "of_passes", "wall_seconds", "tasks", "cases", "context_window"}
)
"""Numeric keys that identify a record rather than measure anything."""

RECORD_IDENTITY = frozenset(
    {"layer", "date", "harness_commit", "model", "quantization", "profile", "base_url"}
)
"""Keys the report reads as identity, so the rest of a record's words are context."""

DEFAULT_COMMAND = "run"
"""`nih bench --layer L2` is a shorthand for `nih bench run --layer L2`."""

HELP_FLAGS = frozenset({"--help", "-h"})
"""Asking for help is not a shorthand for anything."""


class BenchGroup(TyperGroup):
    """A group whose options may come before the subcommand, or instead of one.

    `nih bench --layer L2 --repeat 3` has to keep working next to
    `nih bench report`, so an argument that is not a known subcommand is
    routed to the default one - including no arguments at all, which runs L1.
    A request for help is left alone, or the group's own help would never
    mention `report`.
    """

    def parse_args(self, ctx: Any, args: list[str]) -> list[str]:  # noqa: ANN401 - click Context
        """Insert the default subcommand unless one was named."""
        if not args or (args[0] not in self.commands and args[0] not in HELP_FLAGS):
            args = [DEFAULT_COMMAND, *args]
        parsed: list[str] = super().parse_args(ctx, args)
        return parsed


def read_records(root: Path, *, layer: str | None = None) -> list[ReportRow]:
    """Read every record under `benchmarks/results/`, oldest file first.

    A line that will not parse stops the report instead of being skipped. A
    tool that quietly drops a measurement it could not read is the wrong tool
    to settle a question with.

    Args:
        root: the project root.
        layer: only records from this layer, or every layer when None.

    Returns:
        The records in the order they were written.

    Raises:
        SuiteError: if a line is not a JSON object.
    """
    rows: list[ReportRow] = []
    for path in sorted((root / RESULTS).glob("*.jsonl")):
        lines = path.read_text(encoding="utf-8").splitlines()
        for number, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as error:
                msg = f"{path.name} line {number} is not JSON: {error}"
                raise SuiteError(msg) from error
            if not isinstance(record, dict):
                msg = f"{path.name} line {number} is not a record"
                raise SuiteError(msg)
            if layer and str(record.get("layer", "")).upper() != layer.upper():
                continue
            rows.append(_row(record))
    return rows


def _row(record: Mapping[str, object]) -> ReportRow:
    """Reduce one raw record to the identity, the numbers and the graded units.

    Which keys are metrics is derived rather than declared: a record's metric
    names come from the suite that wrote it, so a list here would be a second
    place for them to disagree.
    """
    metrics = {
        name: float(value)
        for name, value in record.items()
        if isinstance(value, int | float)
        and not isinstance(value, bool)
        and name not in RECORD_METADATA
    }
    context = {
        name: value
        for name, value in record.items()
        if isinstance(value, str) and name not in RECORD_IDENTITY
    }
    units: dict[str, bool] = {}
    for key in ("per_task", "per_case"):
        entries = record.get(key)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if isinstance(entry, dict) and isinstance(entry.get("passed"), bool):
                units[str(entry.get("id", "?"))] = bool(entry["passed"])
    return ReportRow(
        layer=str(record.get("layer", "?")),
        date=str(record.get("date", "")),
        commit=str(record.get("harness_commit", "unknown")),
        served=str(record.get("model", "?")),
        quantization=str(record.get("quantization", "")),
        dirty=bool(record.get("harness_dirty", False)),
        by_hand=bool(record.get("measured_by")),
        context=context,
        metrics=metrics,
        units=units,
    )


def report(root: Path, *, layer: str | None = None, out: Path | None = None) -> None:
    """Render the committed records as Markdown, to a file or to stdout."""
    try:
        rows = read_records(root, layer=layer)
    except SuiteError as error:
        typer.secho(str(error), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from error
    text = report_markdown(rows)
    if out is None:
        typer.echo(text.rstrip())
        return
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    typer.echo(f"wrote {out} - {len(rows)} record(s)")


# ------------------------------------------------------------------------- L3

HIDDEN = "hidden"
"""Directory under a suite holding the tests a task is graded by."""


def load_l3(root: Path) -> tuple[tuple[L3Task, ...], tuple[str, ...]]:
    """Return the L3 tasks and the metric names the suite declares.

    Raises:
        SuiteError: when the file is missing, does not parse, or names a task
            with no hidden tests - which would be a task nothing can grade.
    """
    path = root / SUITES / "l3" / "tasks.toml"
    if not path.is_file():
        msg = f"no L3 suite at {path}"
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
        tasks = tuple(L3Task.model_validate(entry) for entry in raw)
    except ValueError as error:
        msg = f"{path}: {error}"
        raise SuiteError(msg) from error
    hidden = root / SUITES / "l3" / HIDDEN
    missing = [
        task.id
        for task in tasks
        if not any((hidden / task.id).glob("test_*.py"))
        if (hidden / task.id).is_dir()
    ]
    absent = [task.id for task in tasks if not (hidden / task.id).is_dir()]
    if absent or missing:
        named = ", ".join(sorted({*absent, *missing}))
        msg = f"{path}: no hidden tests under {HIDDEN}/ for {named}"
        raise SuiteError(msg)
    metrics = body.get("metrics", {}).get("record", [])
    return tasks, tuple(str(name) for name in metrics)


def plan_of(task: L3Task) -> Plan:
    """Return a one-task plan for this task, bound to a digest of the task itself.

    A plan's `spec_digest` ties it to the spec it came from, and a benchmark
    task has no spec. Digesting the task is the honest substitute: it is still
    a value nothing can transcribe wrongly, and two records of the same task
    carry the same one.
    """
    return Plan(
        spec_digest=digest_json(task.model_dump(mode="json")),
        tasks=(
            Task(
                id=Slug(task.id),
                title=task.title,
                intent=task.intent,
                files=task.files,
                commit_type=task.commit_type,
                acceptance=task.acceptance,
            ),
        ),
    )


def stage_hidden(root: Path, task: L3Task, repo: Path) -> int:
    """Copy a task's hidden tests into the finished worktree and count them.

    After the run, never before: they are the grade, and a worktree that held
    them would be handing the model the answer key.

    Raises:
        SuiteError: when the tests cannot be placed.
    """
    source = root / SUITES / "l3" / HIDDEN / task.id
    tests = repo / "tests"
    try:
        tests.mkdir(parents=True, exist_ok=True)
        staged = 0
        for path in sorted(source.glob("test_*.py")):
            shutil.copy2(path, tests / path.name)
            staged += 1
    except OSError as error:
        msg = f"cannot stage the hidden tests for {task.id}: {error}"
        raise SuiteError(msg) from error
    return staged


def hidden_verdict(repo: Path) -> tuple[bool, str]:
    """Run the tests in `repo` and return whether they passed, with the tail.

    The fixture's own `pyproject.toml` says where its sources are, so this is
    the same command a human would run in that directory - and the same one
    the gate's Python row runs.
    """
    try:
        finished = subprocess.run(
            [sys.executable, "-m", "pytest", "-q"],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=HIDDEN_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return False, f"the hidden tests could not be run: {error}"
    tail = (finished.stdout or finished.stderr).strip().splitlines()
    return finished.returncode == 0, " / ".join(tail[-2:])


HIDDEN_TIMEOUT = 300
"""Seconds for the hidden tests. A task's own work is already budgeted."""


async def run_build(
    config: HarnessConfig,
    task: L3Task,
    *,
    model: ModelConfig,
    backend: object,
    utility: object,
    scratch: Path,
    local_tools: bool,
) -> BuildScore:
    """Run one whole task through the real pipeline, then grade it on hidden tests.

    The pipeline is `ImplementationRun` - the same object `nih implement`
    drives - so what is measured is implement, the commit gate, review and
    bounded fixes as they actually run, rather than a rig that resembles them.
    The only thing this function adds is the grade, and it adds it after the
    run has ended and its commits are made.

    The grade is taken on **`HEAD`** - the committed tree - because that is
    what the gate guarantees and what a human would merge. The first version
    graded the working tree, and its second pass showed why that is wrong: a
    task committed, a fix step then damaged the worktree, and the damage was
    graded instead of the commit.

    When nothing was committed the working tree is graded separately and
    recorded as `uncommitted_would_pass`, because work that was done and never
    offered to the gate is a different failure from work that was wrong.

    Raises:
        SuiteError: when the fixture or the hidden tests cannot be staged.
    """
    worktree = prepare_fixture(config.project_root, scratch / "repo", suite="l3")
    run_id = RunId(f"bench-{task.id}")
    sink = JsonlEventSink(scratch / "runs", run_id)
    vcs = GitVcs(Path(worktree.path))

    async with AsyncExitStack() as stack:
        retrieval = await stack.enter_async_context(open_retrieval(config))
        tools = await stack.enter_async_context(
            connect_tools(
                server_parameters(config, worktree, local=local_tools),
                (),
                limits=config.tools,
                local=retrieval.tools(config, run_id=run_id),
            ),
        )
        runner = StepRunner(
            backend=backend,  # type: ignore[arg-type]
            tools=tools,
            events=sink,
            vcs=vcs,
            worktree=worktree,
        )
        run = ImplementationRun(
            runner=runner,
            events=sink,
            tokenizer=backend,  # type: ignore[arg-type]
            model=model.name,
            policy=config.compact,
            thresholds=config.loop,
            limits=config.tools,
            summarizer=ModelSummarizer(utility, config.models.utility.name),  # type: ignore[arg-type]
            vcs=vcs,
            worktree=worktree,
            catalog=tools.catalog,
            generation_tps=model.measured_generation_tps,
            max_revisions=config.max_revisions,
        )
        outcome = await run.run(
            plan_of(task),
            run_id,
            branch=worktree.branch,
            profile=config.profile,
            tools="local" if local_tools else "sandbox",
            sandbox_network=config.sandbox.network,
        )

    repo = Path(worktree.path)
    payloads = await payloads_of(sink, run_id)
    landed = await vcs.head(worktree)

    # The working tree first, while it still exists, and only to answer "was
    # the work right even though it never reached the gate".
    staged = stage_hidden(config.project_root, task, repo)
    loose, loose_tail = hidden_verdict(repo)

    # `reset --hard` plus `clean -fd`: it takes the staged hidden tests with it,
    # and any file the model wrote and never committed, which is the point.
    await vcs.rollback(worktree, landed)
    stage_hidden(config.project_root, task, repo)
    passed, tail = hidden_verdict(repo)

    scored = score_build(
        task,
        payloads,
        hidden_tests_passed=passed,
        uncommitted_would_pass=loose,
        unfinished=undone(outcome),
    )
    if passed:
        return scored
    detail = tail if scored.committed else loose_tail
    return scored.model_copy(
        update={"notes": (*scored.notes, f"{staged} hidden file(s) on {landed[:7]}: {detail}")}
    )


def undone(outcome: RunResult) -> tuple[str, ...]:
    """Return the Definition of Done items the last answer left unticked.

    `answer.complete` demands every item, and a step without one is handed
    back - so which items a model will not tick decides how often a task can
    finish at all. Read from the result rather than from the log, because the
    log keeps a digest of what the model wrote and not its text.
    """
    answers = [
        step.answer for task in outcome.tasks for step in task.steps if step.answer is not None
    ]
    if not answers:
        return ()
    return tuple(item.statement for item in answers[-1].dod if not item.done)


def record_builds(
    config: HarnessConfig,
    score: L3Score,
    *,
    model: ModelConfig,
    metrics: Sequence[str],
    seconds: float,
    commit: str,
    dirty: bool,
    attempt: int = 1,
    of: int = 1,
) -> dict[str, object]:
    """Return the JSONL record for one end-to-end run."""
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
        "tasks": len(score.builds),
        "pass": attempt,
        "of_passes": of,
        **declared,
        "tools": score.tools(),
        "pathologies": score.pathologies(),
        "unfinished": score.unfinished(),
        "inference_seconds": score.inference_seconds,
        "tool_seconds": score.tool_seconds,
        "overhead_seconds": round(
            max(0.0, seconds - score.inference_seconds - score.tool_seconds), 1
        ),
        "wall_seconds": round(seconds, 1),
        "per_task": [
            {
                "id": build.task_id,
                "passed": build.passed,
                "committed": build.committed,
                "hidden_tests_passed": build.hidden_tests_passed,
                "uncommitted_would_pass": build.uncommitted_would_pass,
                "first_attempt_gate": build.first_attempt_gate,
                "gate_attempts": build.gate_attempts,
                "revisions": build.revisions,
                "handed_back": build.handed_back,
                "calls": build.calls,
                "tokens": build.tokens,
                "tools": build.tools,
                "interventions": build.interventions,
                "pathologies": build.pathologies,
                "unfinished": list(build.unfinished),
                "inference_seconds": build.inference_seconds,
                "tool_seconds": build.tool_seconds,
                "notes": list(build.notes),
            }
            for build in score.builds
        ],
    }


def human_builds(entry: dict[str, object], score: L3Score) -> str:
    """Render an end-to-end run: the metrics, then what each task did."""
    lines = [
        f"L3 on {entry['model']} ({entry['quantization']}), {entry['tasks']} task(s) "
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
    used = score.tools()
    if used:
        detail = ", ".join(f"{name} x{count}" for name, count in used.items())
        lines.append(f"  {'called':<26} {detail}")
    fired = score.pathologies()
    if fired:
        detail = ", ".join(f"{name} x{count}" for name, count in fired.items())
        lines.append(f"  {'intervened on':<26} {detail}")
    stuck = score.unfinished()
    if stuck:
        lines.append(f"  {'left unticked':<26}")
        lines += [f"    x{count}  {item}" for item, count in stuck.items()]
    lines += [
        "",
        f"  inference {score.inference_seconds}s, tools {score.tool_seconds}s, "
        f"harness {entry['overhead_seconds']}s",
        "",
    ]
    for build in score.builds:
        mark = "ok  " if build.passed else "FAIL"
        landed = "committed" if build.committed else "nothing committed"
        lines.append(
            f"  [{mark}] {build.task_id:<36} {landed}, {build.gate_attempts} gate "
            f"attempt(s), {build.revisions} revision(s), {build.calls} call(s)"
        )
        lines += [f"         {note}" for note in build.notes]
    return "\n".join(lines)
