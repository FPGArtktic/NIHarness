# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""`nih init`: probe the hardware, pick a profile, write the project config.

The L0 benchmark that turns measured tok/s into step budgets needs a running
backend, so `init` writes the configuration and says what is still missing
rather than inventing numbers.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import typer
from pydantic import BaseModel, ConfigDict, Field

from niharness.config import PROJECT_FILE

GPU_FULL_VRAM_MB = 20_000
"""From this much VRAM the whole model fits on the GPU."""

HYBRID_MIN_VRAM_MB = 3_000
"""Below this, attention and KV cache do not fit either, so it is CPU only."""

CONTEXT_STEPS: tuple[int, ...] = (65_536, 32_768, 16_384)
"""Shipped context sizes, largest first. 64k is the tested maximum."""

WEIGHTS_MB = 18_000
"""Qwen3-Coder-30B-A3B at Q4_K_M, resident wherever the experts live."""

TOOLCHAIN_RESERVE_MB = 8_000
"""Headroom for the sandbox: compilers, linkers and test runners are not free."""

KV_MB_PER_1K_TOKENS = 48
"""48 layers x 1024 KV dims x 1 byte at q8_0, which is what the profiles use."""


def kv_cache_mb(context_window: int) -> int:
    """Return the KV cache size of `context_window` tokens, in MiB."""
    return context_window * KV_MB_PER_1K_TOKENS // 1_024


def context_window_for(hardware: Hardware) -> int:
    """Return the largest shipped context size this host can hold.

    On a GPU profile the KV cache lives in VRAM, so VRAM is the budget. On
    `cpu-only` it lives in system RAM alongside the weights and whatever the
    sandbox needs, so a machine with plenty of RAM gets the full window - the
    real cost there is prompt-processing time, not memory, and that is a
    budget decision rather than a configuration limit.

    Args:
        hardware: the probed host.

    Returns:
        One of :data:`CONTEXT_STEPS`.
    """
    if hardware.vram_mb >= HYBRID_MIN_VRAM_MB:
        budget = hardware.vram_mb - 1_000
        if hardware.vram_mb >= GPU_FULL_VRAM_MB:
            budget = hardware.vram_mb - WEIGHTS_MB - 1_000
    else:
        budget = hardware.ram_mb - WEIGHTS_MB - TOOLCHAIN_RESERVE_MB
    for size in CONTEXT_STEPS:
        if kv_cache_mb(size) <= budget:
            return size
    return CONTEXT_STEPS[-1]


class Hardware(BaseModel):
    """What the host offers."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    cpu_cores: int = Field(ge=1)
    ram_mb: int = Field(ge=0)
    gpu_name: str = ""
    vram_mb: int = Field(default=0, ge=0)

    @property
    def profile(self) -> str:
        """Return the hardware profile this host should use."""
        if self.vram_mb >= GPU_FULL_VRAM_MB:
            return "gpu-full"
        if self.vram_mb >= HYBRID_MIN_VRAM_MB:
            return "hybrid-small-vram"
        return "cpu-only"


def _ram_mb() -> int:
    """Return total RAM in MiB, read from the kernel, zero when unavailable."""
    meminfo = Path("/proc/meminfo")
    if not meminfo.is_file():
        return 0
    for line in meminfo.read_text(encoding="utf-8").splitlines():
        if line.startswith("MemTotal:"):
            return int(line.split()[1]) // 1024
    return 0


def _gpu() -> tuple[str, int]:
    """Return the first GPU's name and VRAM in MiB, from `nvidia-smi` when present."""
    binary = shutil.which("nvidia-smi")
    if binary is None:
        return "", 0
    try:
        output = subprocess.run(  # noqa: S603 - fixed argv, local binary, no shell
            [binary, "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        ).stdout
    except (subprocess.SubprocessError, OSError):
        return "", 0
    first = output.strip().splitlines()[0] if output.strip() else ""
    if "," not in first:
        return "", 0
    name, _, vram = first.partition(",")
    return name.strip(), int(vram.strip()) if vram.strip().isdigit() else 0


def probe() -> Hardware:
    """Probe the host for cores, memory and GPU."""
    name, vram = _gpu()
    return Hardware(
        cpu_cores=os.cpu_count() or 1,
        ram_mb=_ram_mb(),
        gpu_name=name,
        vram_mb=vram,
    )


def render_project_config(hardware: Hardware) -> str:
    """Render a `harness.toml` for this host."""
    window = context_window_for(hardware)
    # REUSE-IgnoreStart - these lines are the generated file's header, not this file's
    return "\n".join(
        [
            "# SPDX-FileCopyrightText: 2026 Mateusz Okulanis",
            "# SPDX-License-Identifier: GPL-3.0-or-later",
            "#",
            "# Written by `nih init`. Values here override the hardware profile in",
            f"# profiles/{hardware.profile}.toml; NIH_ environment variables win over both.",
            "",
            f'profile = "{hardware.profile}"',
            "",
            "[models.main]",
            f"# KV cache at q8_0: about {kv_cache_mb(window)} MiB for {window} tokens.",
            f"context_window = {window}",
            "",
            "[compact]",
            f"context_window = {window}",
            "threshold = 0.75",
            "keep_last_tool_results = 6",
            "",
            "[loop]",
            "progress_stall_calls = 8",
            "gate_thrash_repeats = 3",
            "",
            "[sandbox]",
            'network = "none"',
            f"cpus = {max(1, hardware.cpu_cores // 2)}.0",
            f'memory = "{max(2, hardware.ram_mb // 2048)}g"',
            "",
        ],
    )
    # REUSE-IgnoreEnd


def init(root: Path, *, force: bool) -> None:
    """Probe the host and write `harness.toml`.

    Raises:
        typer.Exit: when the file exists and `force` was not given.
    """
    hardware = probe()
    typer.echo("hardware:")
    typer.echo(f"  cpu cores: {hardware.cpu_cores}")
    typer.echo(f"  ram: {hardware.ram_mb} MiB")
    typer.echo(f"  gpu: {hardware.gpu_name or 'none detected'} ({hardware.vram_mb} MiB VRAM)")
    typer.echo(f"  profile: {hardware.profile}")
    window = context_window_for(hardware)
    where = "VRAM" if hardware.vram_mb >= HYBRID_MIN_VRAM_MB else "RAM"
    typer.echo(
        f"  context window: {window} tokens "
        f"(KV cache about {kv_cache_mb(window)} MiB at q8_0, held in {where})",
    )

    target = root / PROJECT_FILE
    if target.exists() and not force:
        typer.secho(
            f"{target} exists; pass --force to overwrite", fg=typer.colors.YELLOW, err=True
        )
        raise typer.Exit(1)
    target.write_text(render_project_config(hardware), encoding="utf-8")
    typer.secho(f"{target}: written", fg=typer.colors.GREEN)
    typer.echo("")
    typer.echo("still missing before a run:")
    typer.echo("  - L0 benchmark (needs a running llama-server): `nih bench --layer L0`")
    typer.echo("  - sandbox image (needs Podman): containers/Containerfile.sandbox")
    typer.echo("  - Quadlet units: quadlet/")
