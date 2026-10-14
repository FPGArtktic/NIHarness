# SPDX-FileCopyrightText: 2026 Mateusz Okulanis
# SPDX-License-Identifier: GPL-3.0-or-later
"""Configuration.

Three layers, each overriding the previous one: the shipped defaults, the
hardware profile (`profiles/<name>.toml`), and the project file
(`harness.toml`). Environment variables prefixed `NIH_` win over all of them,
which is how a single run is tweaked without editing a file.

The domain never reads configuration: values are passed in as frozen models,
so a test can construct the exact policy it wants to exercise.
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal, Self

import platformdirs
from pydantic import BaseModel, ConfigDict, Field
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

from niharness.domain.context import CompactionPolicy
from niharness.domain.loop import LoopThresholds
from niharness.domain.pipeline import DEFAULT_MAX_REVISIONS
from niharness.domain.tools import ToolLimits

PROJECT_FILE = "harness.toml"
PROFILE_DIR = "profiles"
STATE_DIR = ".nih"


class ModelConfig(BaseModel):
    """One served model: where it is and what it can do."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    base_url: str = "http://127.0.0.1:8080"
    context_window: int = Field(default=65_536, ge=4_096)
    supports_grammar: bool = True
    supports_reasoning: bool = False
    quantization: str = Field(default="Q4_K_M", description="The floor is Q4_K_M.")
    measured_generation_tps: float = Field(
        default=0.0,
        ge=0.0,
        description="Written by `nih init` from the L0 benchmark; step budgets derive from it.",
    )
    measured_prompt_tps: float = Field(default=0.0, ge=0.0)


class ModelsConfig(BaseModel):
    """The models of a profile. One main model per run, no swaps inside a run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    main: ModelConfig = ModelConfig(name="Qwen3-Coder-30B-A3B-Instruct")
    thinking: ModelConfig | None = None
    utility: ModelConfig = ModelConfig(
        name="BitNet-b1.58-2B-4T",
        base_url="http://127.0.0.1:8081",
        context_window=4_096,
        supports_grammar=False,
        quantization="i2_s",
    )
    embeddings: ModelConfig | None = None


class SandboxConfig(BaseModel):
    """How the tool sandbox is started. `--network=none` is not negotiable."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    image: str = "localhost/niharness-sandbox:latest"
    network: Literal["none"] = "none"
    cpus: float = Field(default=4.0, gt=0)
    memory: str = "8g"
    quick_timeout_seconds: int = Field(default=900, ge=1)
    full_timeout_seconds: int = Field(default=10_800, ge=1)


class VectorConfig(BaseModel):
    """Where the vector store lives and how collections are named."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    url: str = "http://127.0.0.1:6333"
    code_collection: str = "code"
    run_collection_prefix: str = "run-"
    embedding_dimensions: int = Field(default=1_024, ge=64)


class GateConfig(BaseModel):
    """Gate behaviour. The matrix itself lives in the domain."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    disabled_languages: tuple[str, ...] = ()
    fail_fast: bool = Field(
        default=True,
        description="Stop at the first red command; the signature stays stable either way.",
    )


class HarnessConfig(BaseSettings):
    """The whole configuration of one harness instance."""

    model_config = SettingsConfigDict(
        env_prefix="NIH_",
        env_nested_delimiter="__",
        frozen=True,
        extra="forbid",
    )

    project_root: Path = Field(default=Path())
    profile: str = Field(default="hybrid-small-vram")
    max_revisions: int = Field(default=DEFAULT_MAX_REVISIONS, ge=0)
    models: ModelsConfig = ModelsConfig()
    sandbox: SandboxConfig = SandboxConfig()
    vector: VectorConfig = VectorConfig()
    gate: GateConfig = GateConfig()
    loop: LoopThresholds = LoopThresholds()
    compact: CompactionPolicy = CompactionPolicy()
    tools: ToolLimits = ToolLimits()

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Put the environment ahead of the merged files, which arrive as init values."""
        del settings_cls, dotenv_settings
        return (env_settings, init_settings, file_secret_settings)

    @property
    def state_dir(self) -> Path:
        """Where run state lives: event logs, snapshots, worktrees, approvals."""
        return self.project_root / STATE_DIR

    @property
    def runs_dir(self) -> Path:
        """Where event logs are stored, one directory per run."""
        return self.state_dir / "runs"

    @property
    def worktrees_dir(self) -> Path:
        """Where per-run worktrees are created."""
        return self.state_dir / "worktrees"

    @property
    def approved_plan_path(self) -> Path:
        """The approval marker `nih implement` reads."""
        return self.state_dir / "approved-plan.md"

    @property
    def cache_dir(self) -> Path:
        """XDG cache directory, for anything derived and discardable."""
        return Path(platformdirs.user_cache_dir("niharness"))

    @classmethod
    def load(
        cls,
        project_root: Path | None = None,
        *,
        profile: str | None = None,
        overrides: Mapping[str, Any] | None = None,
    ) -> Self:
        """Load configuration for a project.

        Args:
            project_root: the project directory; the current directory by default.
            profile: hardware profile name; the one named in `harness.toml`
                otherwise, falling back to the shipped default.
            overrides: values applied last, used by the CLI for flags.

        Returns:
            The merged, validated configuration.
        """
        root = (project_root or Path.cwd()).resolve()
        project = _read_toml(root / PROJECT_FILE)
        chosen = profile or str(project.get("profile", cls.model_fields["profile"].default))
        profile_data = _read_toml(root / PROFILE_DIR / f"{chosen}.toml")
        # Start from the shipped defaults so a file may override one key of a
        # section without having to restate the whole section.
        merged = cls(project_root=root).model_dump(mode="json")
        for layer in (profile_data, project, dict(overrides or {})):
            merged = _deep_merge(merged, layer)
        merged["project_root"] = root
        merged["profile"] = chosen
        return cls(**merged)


def _read_toml(path: Path) -> dict[str, Any]:
    """Return a TOML file as a dict, or an empty dict when it does not exist."""
    if not path.is_file():
        return {}
    with path.open("rb") as handle:
        return tomllib.load(handle)


def _deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    """Merge `override` into `base` recursively, without mutating either."""
    merged: dict[str, Any] = dict(base)
    for key, value in override.items():
        current = merged.get(key)
        if isinstance(current, Mapping) and isinstance(value, Mapping):
            merged[key] = _deep_merge(current, value)
        else:
            merged[key] = value
    return merged
