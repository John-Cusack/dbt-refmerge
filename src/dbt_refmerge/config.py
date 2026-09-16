"""Configuration: precedence CLI > DBT_REFMERGE_* env > .dbt-refmerge.toml > defaults."""

from __future__ import annotations

import os
import shlex
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
_ENV_PREFIX = "DBT_REFMERGE_"
_ENV_BOOL_FIELDS = ("keep_workspace", "allow_compile_introspection", "json_output", "debug")


def is_secret_name(name: str) -> bool:
    lowered = name.lower()
    return any(h in lowered for h in _SECRET_HINTS)


def _check_external_string(value: str, field: str, max_len: int = 1024) -> str:
    if any(ord(c) < 32 and c != "\t" for c in value):
        raise ValueError(f"{field} contains NUL/control characters")
    if len(value) > max_len:
        raise ValueError(f"{field} exceeds length limit")
    return value


def _read_toml(path: Path) -> dict[str, Any]:
    """Settings from ``[tool.dbt-refmerge]`` when the file has that table, else its top-level keys.

    A ``tool`` entry that is not a table, or has no ``dbt-refmerge`` key, belongs to another tool and
    means "no tool table". A ``tool.dbt-refmerge`` entry that is not a table is refused.
    """
    if not path.is_file():
        return {}
    with path.open("rb") as fh:
        raw = tomllib.load(fh)
    tool = raw.get("tool")
    if not isinstance(tool, dict) or "dbt-refmerge" not in tool:
        return raw
    section = tool["dbt-refmerge"]
    if not isinstance(section, dict):
        raise ValueError(f"{path}: [tool.dbt-refmerge] must be a table")
    return section


def load_config(
    project_dir: Path | str = ".",
    cli_overrides: dict[str, Any] | None = None,
) -> AppConfig:
    """Load config with precedence: CLI overrides > env > .dbt-refmerge.toml > defaults.

    ``DBT_REFMERGE_<FIELD>`` sets ``<field>``. A ``dbt_command`` given as a string, in the environment or
    the TOML file, is split like a POSIX shell would (``shlex.split``), so quote paths containing spaces.
    Every invalid configuration raises ``ValueError`` (pydantic's ``ValidationError`` and
    ``tomllib.TOMLDecodeError`` are subclasses of it).
    """
    root = Path(project_dir)
    merged: dict[str, Any] = {"project_dir": str(root)}
    merged.update(_read_toml(root / ".dbt-refmerge.toml"))
    for key, value in os.environ.items():
        if not key.startswith(_ENV_PREFIX):
            continue
        field = key[len(_ENV_PREFIX) :].lower()
        # Numbers stay strings: pydantic parses them and names the field when it cannot.
        merged[field] = value.lower() in ("1", "true", "yes") if field in _ENV_BOOL_FIELDS else value
    if cli_overrides:
        merged.update({k: v for k, v in cli_overrides.items() if v is not None})

    command = merged.get("dbt_command")
    if isinstance(command, str):
        try:
            merged["dbt_command"] = tuple(shlex.split(command))
        except ValueError as exc:
            raise ValueError(f"dbt_command: {exc}") from None

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
    # dbt runs inside a snapshot, not the caller's working directory, so hand it an absolute profiles dir.
    # dbt resolves a relative DBT_PROFILES_DIR against its working directory, i.e. the project root.
    profiles_dir = config.profiles_dir
    env_profiles_dir = os.environ.get("DBT_PROFILES_DIR")
    if profiles_dir is None and env_profiles_dir:
        profiles_dir = resolved / env_profiles_dir
    if profiles_dir is not None:
        object.__setattr__(config, "profiles_dir", profiles_dir.resolve())
    return config
