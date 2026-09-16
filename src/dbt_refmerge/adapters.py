"""Adapter resolution: CLI override > manifest > profiles.yml > fail closed.

The dbt adapter type determines the SQL dialect (sqlglot parsing), the
identifier case-folding rule, and whether exact warehouse verification is
available. v0.1 verifies ``postgres`` only; every other adapter fails closed.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from dbt_refmerge.domain import ReasonCode
from dbt_refmerge.errors import RefmergeError
from dbt_refmerge.verification.capabilities import (
    POSTGRES_CAPABILITIES,
    VerificationCapabilities,
)

FoldRule = Literal["lower", "upper", "none"]


@dataclass(frozen=True)
class AdapterSpec:
    """Everything the tool needs to know about one dbt adapter."""

    name: str  # canonical adapter key, e.g. "postgres"
    sqlglot_dialect: str
    fold_unquoted: FoldRule
    verifies: bool  # False → scan/parse only; check fails closed
    capabilities: VerificationCapabilities | None


ADAPTER_SPECS: dict[str, AdapterSpec] = {
    "postgres": AdapterSpec(
        name="postgres",
        sqlglot_dialect="postgres",
        fold_unquoted="lower",
        verifies=True,
        capabilities=POSTGRES_CAPABILITIES,
    ),
    # Known but unverified in v0.1: resolve for scan folding, fail closed at check.
    "snowflake": AdapterSpec(
        name="snowflake",
        sqlglot_dialect="snowflake",
        fold_unquoted="upper",
        verifies=False,
        capabilities=None,
    ),
    "duckdb": AdapterSpec(
        name="duckdb",
        sqlglot_dialect="duckdb",
        fold_unquoted="lower",
        verifies=False,
        capabilities=None,
    ),
    # BigQuery CTEs resolve case-insensitively (sqlglot NormalizationStrategy
    # .CASE_INSENSITIVE); lower-fold is the grouping proxy for scan only.
    "bigquery": AdapterSpec(
        name="bigquery",
        sqlglot_dialect="bigquery",
        fold_unquoted="lower",
        verifies=False,
        capabilities=None,
    ),
    # Databricks SQL resolves unquoted identifiers case-insensitively
    # (Spark semantics); lower-fold is the grouping proxy for scan only.
    "databricks": AdapterSpec(
        name="databricks",
        sqlglot_dialect="databricks",
        fold_unquoted="lower",
        verifies=False,
        capabilities=None,
    ),
    # PostgreSQL-derived or compatible: unquoted folds to lowercase.
    "redshift": AdapterSpec("redshift", "redshift", "lower", False, None),
    "materialize": AdapterSpec("materialize", "materialize", "lower", False, None),
    # Trino-family engines fold unquoted identifiers to lowercase.
    "trino": AdapterSpec("trino", "trino", "lower", False, None),
    "presto": AdapterSpec("presto", "presto", "lower", False, None),
    "athena": AdapterSpec("athena", "athena", "lower", False, None),
    # Spark-family engines resolve unquoted identifiers case-insensitively.
    "spark": AdapterSpec("spark", "spark", "lower", False, None),
    # Case-insensitive engines with lowercase grouping proxy.
    "sqlite": AdapterSpec("sqlite", "sqlite", "lower", False, None),
    "tsql": AdapterSpec("tsql", "tsql", "lower", False, None),
    # Oracle-family engines fold unquoted identifiers to uppercase.
    "oracle": AdapterSpec("oracle", "oracle", "upper", False, None),
    "exasol": AdapterSpec("exasol", "exasol", "upper", False, None),
    # ClickHouse identifiers are case-sensitive: preserve exact spelling,
    # otherwise scan would merge distinct columns.
    "clickhouse": AdapterSpec("clickhouse", "clickhouse", "none", False, None),
}

_ADAPTER_ALIASES = {
    "postgresql": "postgres",
    "pg": "postgres",
}

_DIALECT_TO_SPEC = {spec.sqlglot_dialect: spec for spec in ADAPTER_SPECS.values()}

MAX_PROFILES_BYTES = 1_048_576


def canonical_adapter_name(raw: str) -> str:
    """Normalize a user- or file-supplied adapter name to its canonical key."""
    name = raw.strip().lower()
    if not name or len(name) > 64 or "\x00" in name:
        raise RefmergeError(ReasonCode.UNSUPPORTED_ADAPTER, f"invalid adapter name: {raw!r}")
    return _ADAPTER_ALIASES.get(name, name)


def get_spec(name: str) -> AdapterSpec:
    """Return the spec for a canonical adapter name, or fail closed."""
    spec = ADAPTER_SPECS.get(name)
    if spec is None:
        supported = sorted(key for key, item in ADAPTER_SPECS.items() if item.verifies)
        known = sorted(key for key, item in ADAPTER_SPECS.items() if not item.verifies)
        raise RefmergeError(
            ReasonCode.UNSUPPORTED_ADAPTER,
            f"unsupported adapter {name!r}; v0.1 supports: {', '.join(supported)} "
            f"(known but unverified: {', '.join(known)})",
        )
    return spec


def spec_for_dialect(dialect: str) -> AdapterSpec:
    """Reverse lookup for sqlglot dialect names produced by resolved specs."""
    spec = _DIALECT_TO_SPEC.get(dialect)
    if spec is None:
        raise RefmergeError(ReasonCode.UNSUPPORTED_ADAPTER, f"unsupported SQL dialect: {dialect!r}")
    return spec


def fold_identity(source_text: str, quoted: bool, fold_unquoted: FoldRule) -> str:
    """Adapter-aware identifier normalization (resolved spelling)."""
    if quoted or fold_unquoted == "none":
        return source_text
    return source_text.lower() if fold_unquoted == "lower" else source_text.upper()


def read_project_profile(project_dir: Path) -> str | None:
    """Return the `profile:` pointer from dbt_project.yml/yaml, or None."""
    import yaml

    for filename in ("dbt_project.yml", "dbt_project.yaml"):
        candidate = project_dir / filename
        if not candidate.is_file():
            continue
        try:
            if candidate.stat().st_size > MAX_PROFILES_BYTES:
                return None
            raw = yaml.safe_load(candidate.read_text(encoding="utf-8"))
        except Exception:
            return None
        if isinstance(raw, dict):
            profile = raw.get("profile")
            if isinstance(profile, str) and profile.strip():
                return profile.strip()
        return None
    return None


def _default_profiles_dir(project_dir: Path) -> Path:
    """Where dbt looks for profiles.yml when no --profiles-dir is given.

    dbt's order is ``DBT_PROFILES_DIR`` (ignored when empty), then the working directory if it holds a
    ``profiles.yml``, then ``~/.dbt``. dbt-refmerge runs dbt from (a snapshot of) the project directory,
    so the project directory stands in for the working directory, including for a relative
    ``DBT_PROFILES_DIR``. dbt does not fall back once a directory is chosen, and neither does the reader.
    """
    env_dir = os.environ.get("DBT_PROFILES_DIR")
    if env_dir:
        return project_dir / env_dir
    if (project_dir / "profiles.yml").exists():
        return project_dir
    return Path.home() / ".dbt"


def read_profiles_target_type(profiles_dir: Path, profile: str, target: str | None) -> str | None:
    """Return the `type:` of a target in ``<profiles_dir>/profiles.yml``, or None when unavailable.

    Mirrors dbt: only ``profiles.yml`` is read, and the target is the override, else the profile's
    ``target:`` key whenever present (dbt renders it; unrendered Jinja finds no output here), else
    ``default``. An empty override counts as none because the tool never passes an empty --target.

    Fail-soft by design: absence of evidence is not evidence of an adapter.
    The resolution chain decides whether None is fatal.
    """
    import yaml

    candidate = profiles_dir / "profiles.yml"
    try:
        if not candidate.is_file() or candidate.stat().st_size > MAX_PROFILES_BYTES:
            return None
        raw = yaml.safe_load(candidate.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(raw, dict):
        return None
    entry = raw.get(profile)
    if not isinstance(entry, dict):
        return None
    outputs = entry.get("outputs")
    if not isinstance(outputs, dict):
        return None
    target_name = target or entry.get("target", "default")
    if not isinstance(target_name, str) or target_name not in outputs:
        return None
    output = outputs[target_name]
    if not isinstance(output, dict):
        return None
    adapter_type = output.get("type")
    if not isinstance(adapter_type, str) or not adapter_type.strip():
        return None
    return adapter_type.strip()


def resolve_adapter(
    *,
    cli_override: str | None,
    manifest_adapter: str | None,
    project_dir: Path,
    profiles_dir: Path | None,
    profile: str | None,
    target: str | None,
) -> AdapterSpec:
    """Resolve the canonical adapter spec.

    Precedence: CLI override > manifest adapter_type > profiles.yml target type.
    Manifest/profiles disagreement (or CLI/manifest disagreement) is
    ADAPTER_MISMATCH: a stale manifest or wrong target must never be silently
    analyzed as the wrong dialect.
    """
    manifest_name = canonical_adapter_name(manifest_adapter) if manifest_adapter else None
    cli_name = canonical_adapter_name(cli_override) if cli_override else None

    profiles_name: str | None = None
    profile_name = profile or read_project_profile(project_dir)
    if profile_name:
        base = profiles_dir if profiles_dir is not None else _default_profiles_dir(project_dir)
        profiles_type = read_profiles_target_type(base, profile_name, target)
        if profiles_type:
            profiles_name = canonical_adapter_name(profiles_type)

    if manifest_name is not None and profiles_name is not None and manifest_name != profiles_name:
        raise RefmergeError(
            ReasonCode.ADAPTER_MISMATCH,
            f"manifest adapter {manifest_name!r} conflicts with profiles.yml target type "
            f"{profiles_name!r}; recompile for the selected target or pass --adapter explicitly",
        )
    if cli_name is not None and manifest_name is not None and cli_name != manifest_name:
        raise RefmergeError(
            ReasonCode.ADAPTER_MISMATCH,
            f"--adapter {cli_name!r} conflicts with manifest adapter {manifest_name!r}; "
            "the manifest was compiled for a different warehouse",
        )
    resolved = cli_name or manifest_name or profiles_name
    if resolved is None:
        raise RefmergeError(
            ReasonCode.UNSUPPORTED_ADAPTER,
            "could not determine the dbt adapter from the manifest or profiles.yml; "
            "pass --adapter explicitly (v0.1 supports: postgres)",
        )
    return get_spec(resolved)
