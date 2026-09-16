"""Configuration: precedence CLI > DBT_REFMERGE_* env > .dbt-refmerge.toml > defaults."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator


class FailOn(str, Enum):
    NEVER = "never"
    FINDING = "finding"
    FIXABLE = "fixable"
    DIFFERENT = "different"
    UNVERIFIABLE = "unverifiable"


class AppConfig(BaseModel):
    project_dir: Path
    profiles_dir: Path | None = None
    profile: str | None = None
    target: str | None = None
    dbt_command: tuple[str, ...] = ("dbt",)
    adapter: str | None = None
    scratch_database: str | None = None
    scratch_schema: str | None = None
    subprocess_timeout_seconds: int = 1800
    warehouse_statement_timeout_ms: int = 900_000
    warehouse_lock_timeout_ms: int = 10_000
    max_planner_total_cost: Decimal | None = None
    fail_on: FailOn = FailOn.FIXABLE
    keep_workspace: bool = False
    allow_compile_introspection: bool = False
    json_output: bool = False
    debug: bool = False

    model_config = ConfigDict(frozen=True)

    @field_validator("project_dir", mode="before")
    @classmethod
    def _coerce_project_dir(cls, v: Any) -> Any:
        return Path(v) if not isinstance(v, Path) else v

    @field_validator("subprocess_timeout_seconds", "warehouse_statement_timeout_ms", "warehouse_lock_timeout_ms")
    @classmethod
    def _positive(cls, v: int) -> int:
        if v <= 0 or v > 86_400_000:
            raise ValueError("timeout must be a positive bounded duration")
        return v


@dataclass(frozen=True)
class CompilationContext:
    dbt_executable: Path
    dbt_version_text: str
    project_dir: Path
    profiles_dir: Path | None
    profile: str
    target: str | None
    vars_json: str | None
    passthrough_args: tuple[str, ...]
    environment_names: tuple[str, ...]
    package_state_sha256: str
    adapter: str = "postgres"


_SECRET_HINTS = ("secret", "password", "token", "key")


def is_secret_name(name: str) -> bool:
    lowered = name.lower()
    return any(h in lowered for h in _SECRET_HINTS)


def redact_mapping(mapping: dict[str, str]) -> dict[str, str]:
    return {k: ("***" if is_secret_name(k) else v) for k, v in mapping.items()}


def _check_external_string(value: str, field: str, max_len: int = 1024) -> str:
    if "\x00" in value or any(ord(c) < 32 and c not in ("\t",) for c in value):
        raise ValueError(f"{field} contains NUL/control characters")
    if len(value) > max_len:
        raise ValueError(f"{field} exceeds length limit")
    return value


def load_config(
    project_dir: Path | str = ".",
    cli_overrides: dict[str, Any] | None = None,
) -> AppConfig:
    """Load config with precedence: CLI overrides > env > .dbt-refmerge.toml > defaults."""
    root = Path(project_dir)
    file_values: dict[str, Any] = {}
    toml_path = root / ".dbt-refmerge.toml"
    if toml_path.is_file():
        with toml_path.open("rb") as fh:
            raw = tomllib.load(fh)
        if isinstance(raw, dict):
            tool = raw.get("tool", {}).get("dbt-refmerge", raw)
            if isinstance(tool, dict):
                file_values = dict(tool)

    env_values: dict[str, Any] = {}
    prefix = "DBT_REFMERGE_"
    for key, val in os.environ.items():
        if not key.startswith(prefix):
            continue
        field = key[len(prefix) :].lower()
        env_values[field] = val

    merged: dict[str, Any] = {"project_dir": str(root)}
    merged.update(file_values)
    # env keys are lowercase field names; coerce numerics/bools
    for k, v in env_values.items():
        if k in (
            "subprocess_timeout_seconds",
            "warehouse_statement_timeout_ms",
            "warehouse_lock_timeout_ms",
        ):
            try:
                merged[k] = int(v)
            except ValueError:
                merged[k] = v
        elif k in ("keep_workspace", "allow_compile_introspection", "json_output", "debug"):
            merged[k] = str(v).lower() in ("1", "true", "yes")
        elif k == "dbt_command":
            merged[k] = tuple(str(v).split())
        else:
            merged[k] = v
    if cli_overrides:
        merged.update({k: v for k, v in cli_overrides.items() if v is not None})

    if "dbt_command" in merged and isinstance(merged["dbt_command"], list):
        merged["dbt_command"] = tuple(merged["dbt_command"])
    if "dbt_command" in merged and isinstance(merged["dbt_command"], str):
        merged["dbt_command"] = tuple(merged["dbt_command"].split())

    config = AppConfig(**merged)
    # validate external strings
    if config.profile is not None:
        _check_external_string(config.profile, "profile", 128)
    if config.target is not None:
        _check_external_string(config.target, "target", 128)
    if config.scratch_schema is not None:
        _check_external_string(config.scratch_schema, "scratch_schema", 128)
    if config.adapter is not None:
        _check_external_string(config.adapter, "adapter", 64)
    resolved = config.project_dir.resolve()
    if not (resolved / "dbt_project.yml").is_file() and not (resolved / "dbt_project.yaml").is_file():
        raise ValueError(f"dbt_project.yml not found at {resolved}")
    # resolve without following untrusted final symlink: reject if project_dir itself is a symlink
    if config.project_dir.is_symlink():
        raise ValueError("project_dir must not be an untrusted final symlink")
    object.__setattr__(config, "project_dir", resolved)
    _check_external_string(str(resolved), "project_dir", 4096)
    return config


def require_scratch_schema(config: AppConfig) -> str:
    if not config.scratch_schema:
        raise ValueError("scratch_schema is required for check/fix")
    return config.scratch_schema
