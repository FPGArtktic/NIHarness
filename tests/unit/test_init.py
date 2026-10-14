# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""Hardware probing decides the profile and the context window."""

from pathlib import Path

import pytest

from niharness.cli.init import (
    CONTEXT_STEPS,
    Hardware,
    context_window_for,
    kv_cache_mb,
    render_project_config,
)
from niharness.config import HarnessConfig


def host(ram_mb: int = 32_000, vram_mb: int = 0, cores: int = 8) -> Hardware:
    return Hardware(
        cpu_cores=cores,
        ram_mb=ram_mb,
        gpu_name="Test GPU" if vram_mb else "",
        vram_mb=vram_mb,
    )


@pytest.mark.parametrize(
    ("vram_mb", "profile"),
    [(0, "cpu-only"), (2_048, "cpu-only"), (4_096, "hybrid-small-vram"), (24_576, "gpu-full")],
)
def test_vram_decides_the_profile(vram_mb: int, profile: str) -> None:
    assert host(vram_mb=vram_mb).profile == profile


def test_a_cpu_only_host_with_plenty_of_ram_gets_the_full_window() -> None:
    assert context_window_for(host(ram_mb=64_000)) == 65_536


def test_a_cpu_only_host_with_little_ram_gets_a_smaller_window() -> None:
    assert context_window_for(host(ram_mb=28_000)) == 32_768
    assert context_window_for(host(ram_mb=27_000)) == 16_384


def test_the_window_never_goes_below_the_smallest_shipped_size() -> None:
    assert context_window_for(host(ram_mb=1_024)) == CONTEXT_STEPS[-1]


def test_on_a_gpu_profile_vram_is_the_budget_not_ram() -> None:
    plenty_of_ram_small_card = host(ram_mb=128_000, vram_mb=4_096)
    assert context_window_for(plenty_of_ram_small_card) == 65_536
    tiny_card = host(ram_mb=128_000, vram_mb=3_000)
    assert context_window_for(tiny_card) == 32_768


def test_kv_cache_arithmetic_is_stated_in_mib() -> None:
    assert kv_cache_mb(65_536) == 3_072
    assert kv_cache_mb(32_768) == 1_536


def test_the_rendered_config_loads_back(tmp_path: Path) -> None:
    (tmp_path / "harness.toml").write_text(
        render_project_config(host(ram_mb=64_000, vram_mb=4_096)),
        encoding="utf-8",
    )
    config = HarnessConfig.load(tmp_path)
    assert config.profile == "hybrid-small-vram"
    assert config.models.main.context_window == 65_536
    assert config.compact.context_window == 65_536
    assert config.sandbox.network == "none"


def test_the_rendered_config_carries_its_spdx_header() -> None:
    assert render_project_config(host()).startswith("# SPDX-FileCopyrightText:")
