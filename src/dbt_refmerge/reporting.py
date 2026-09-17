"""Reporting: human rendering, versioned JSON, exit-code policy."""

from __future__ import annotations

from enum import Enum, IntEnum
from pathlib import Path
from typing import Any

from dbt_refmerge.analyze import Finding
from dbt_refmerge.config import FailOn
from dbt_refmerge.domain import ReasonCode, VerificationReceipt, VerificationStatus, is_fixable
from dbt_refmerge.orchestrator import CheckReport, ScanReport


class OutputFormat(str, Enum):
    TEXT = "text"
    JSON = "json"
    GITHUB = "github"


class ExitCode(IntEnum):
    OK = 0
    OPERATIONAL_ERROR = 1
    POLICY_FINDING = 2
    DIFFERENT = 3
    UNVERIFIABLE = 4
    INTERRUPTED = 130


# Explicit severity tables (never derived from enum ordering). A result is classified by the most severe
# --fail-on level it reaches; the same ranks order the --fail-on thresholds.
_SEVERITY: dict[FailOn, int] = {
    FailOn.FINDING: 1,
    FailOn.FIXABLE: 2,
    FailOn.DIFFERENT: 3,
    FailOn.UNVERIFIABLE: 4,
}
_EXIT_CODE: dict[FailOn, ExitCode] = {
    FailOn.FINDING: ExitCode.POLICY_FINDING,
    FailOn.FIXABLE: ExitCode.POLICY_FINDING,
    FailOn.DIFFERENT: ExitCode.DIFFERENT,
    FailOn.UNVERIFIABLE: ExitCode.UNVERIFIABLE,
}
_STATUS_LEVEL: dict[VerificationStatus, FailOn] = {
    VerificationStatus.SNAPSHOT_EQUIVALENT: FailOn.FIXABLE,
    VerificationStatus.DIFFERENT: FailOn.DIFFERENT,
    VerificationStatus.UNVERIFIABLE: FailOn.UNVERIFIABLE,
    VerificationStatus.ERROR: FailOn.UNVERIFIABLE,
}

JSON_SCHEMA_VERSION = "1"


def _result_level(receipt: VerificationReceipt) -> FailOn | None:
    """The --fail-on level a result reaches, or None when the model has nothing to report."""
    if receipt.status == VerificationStatus.NOT_RUN:
        return None if receipt.reason_codes == (ReasonCode.NO_DUPLICATE_IMPORT,) else FailOn.FINDING
    return _STATUS_LEVEL[receipt.status]


def evaluate_exit_code_for_check(report: CheckReport, fail_on: FailOn) -> ExitCode:
    """Exit code for ``check``: ``fail_on`` is the minimum result severity that fails the run.

    Results are ranked finding < fixable < different < unverifiable. ``--fail-on X`` fails the run when
    any result ranks at or above X, and ``--fail-on never`` never fails it. A failing run exits with the
    code of its most severe result, so a lesser result can never mask a greater one:

    ==========================================  =====  =======  =======  =========  ============
    result                                      never  finding  fixable  different  unverifiable
    ==========================================  =====  =======  =======  =========  ============
    not_run with only NO_DUPLICATE_IMPORT       0      0        0        0          0
    not_run with any other reason (finding)     0      2        0        0          0
    snapshot_equivalent (fixable)               0      2        2        0          0
    different                                   0      3        3        3          0
    unverifiable or error                       0      4        4        4          4
    ==========================================  =====  =======  =======  =========  ============
    """
    if fail_on == FailOn.NEVER:
        return ExitCode.OK
    levels = [level for result in report.results if (level := _result_level(result.receipt)) is not None]
    worst = max(levels, key=_SEVERITY.__getitem__, default=None)
    if worst is None or _SEVERITY[worst] < _SEVERITY[fail_on]:
        return ExitCode.OK
    return _EXIT_CODE[worst]


def check_report_json(
    report: CheckReport,
    *,
    command: str,
    project_dir: Path,
    dbt_version: str = "",
    adapter_type: str = "postgres",
    manifest_schema_version: str = "",
    workspace: Path | None = None,
) -> dict[str, Any]:
    models: list[dict[str, Any]] = []
    counts = {
        "models_scanned": len(report.results),
        "findings": 0,
        "fixable": 0,
        "different": 0,
        "unverifiable": 0,
        "errors": 0,
    }
    for result in sorted(report.results, key=lambda r: r.model_unique_id):
        receipt = result.receipt
        fixable = is_fixable(receipt)
        if receipt.status == VerificationStatus.SNAPSHOT_EQUIVALENT:
            counts["findings"] += 1
        if fixable:
            counts["fixable"] += 1
        if receipt.status == VerificationStatus.DIFFERENT:
            counts["different"] += 1
        if receipt.status == VerificationStatus.UNVERIFIABLE:
            counts["unverifiable"] += 1
        if receipt.status == VerificationStatus.ERROR:
            counts["errors"] += 1
        models.append(
            {
                "model_unique_id": receipt.model_unique_id,
                "source_path": receipt.source_path.as_posix(),
                "status": receipt.status.value,
                "reason_codes": [c.value for c in receipt.reason_codes],
                "warning_codes": list(receipt.warning_codes),
                "fixable": fixable,
                "equality": {
                    "schema_equal": receipt.equality.schema_equal,
                    "baseline_rows": receipt.equality.baseline_rows,
                    "candidate_rows": receipt.equality.candidate_rows,
                    "baseline_only_occurrences": receipt.equality.baseline_only_occurrences,
                    "candidate_only_occurrences": receipt.equality.candidate_only_occurrences,
                },
                "diff": result.diff,
                "scratch_relations": [
                    {
                        "database": r.database.value,
                        "schema": r.schema.value,
                        "identifier": r.identifier.value,
                    }
                    for r in receipt.scratch_relations
                ],
            }
        )
    return {
        "schema_version": JSON_SCHEMA_VERSION,
        "command": command,
        "run_id": report.run_id,
        "project_dir": str(project_dir),
        "dbt": {
            "version": dbt_version,
            "adapter_type": adapter_type,
            "manifest_schema_version": manifest_schema_version,
        },
        "summary": counts,
        "models": models,
        "workspace": None if workspace is None else str(workspace),
        "cleanup": {"complete": all(r.receipt.cleanup_complete for r in report.results), "objects": []},
    }


def scan_report_json(report: ScanReport, *, project_dir: Path) -> dict[str, Any]:
    return {
        "schema_version": JSON_SCHEMA_VERSION,
        "command": "scan",
        "project_dir": str(project_dir),
        "summary": {"findings": len(report.findings)},
        "findings": [
            {
                "model_unique_id": f.model_unique_id,
                "source_path": str(f.source_path),
                "upstream_unique_id": f.upstream_unique_id,
                "cte_names": list(f.cte_names),
                "status": f.status.value,
                "reason_codes": [c.value for c in f.reason_codes],
                "line": f.line,
            }
            for f in report.findings
        ],
    }


def finding_message(finding: Finding) -> str:
    if ReasonCode.UNUSED_IMPORT_COLUMNS in finding.reason_codes:
        return (
            f"CTEs {', '.join(finding.cte_names)} can select only needed columns; "
            "verification is available on PostgreSQL only"
        )
    if finding.cte_names:
        target = finding.upstream_unique_id or "the same relation"
        return f"CTEs {', '.join(finding.cte_names)} import {target}; run dbt-refmerge check to prove a merge"
    codes = ", ".join(code.value for code in finding.reason_codes)
    return f"several CTEs import the same relation in a shape dbt-refmerge cannot merge ({codes})"


def render_human_scan(report: ScanReport, *, project_dir: Path) -> str:
    """One ``path:line: message`` lead per line, paths relative to the project."""
    return "".join(
        f"{_display_path(f.source_path, project_dir)}:{f.line}: {finding_message(f)}\n" for f in report.findings
    )


def render_github_scan(report: ScanReport, *, base_dir: Path) -> str:
    """GitHub Actions workflow commands: one warning annotation per lead, on the first duplicated import.

    GitHub resolves ``file`` against the repository root, so paths are relative to ``base_dir`` (the
    workspace) when the model is inside it.
    """
    return "".join(
        f"::warning file={_escape_property(_display_path(f.source_path, base_dir))},line={f.line},"
        f"title={_escape_property(_finding_title(f))}::{_escape_data(finding_message(f))}\n"
        for f in report.findings
    )


def _finding_title(finding: Finding) -> str:
    if ReasonCode.UNUSED_IMPORT_COLUMNS in finding.reason_codes:
        return "dbt-refmerge: unused import columns"
    return "dbt-refmerge: duplicate import CTEs"


def _display_path(path: Path, base_dir: Path) -> str:
    try:
        return path.resolve().relative_to(base_dir.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _escape_data(text: str) -> str:
    # The escaping of GitHub's @actions/core toolkit: messages keep ':' and ',' but not '%' or line breaks.
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _escape_property(text: str) -> str:
    return _escape_data(text).replace(":", "%3A").replace(",", "%2C")


def render_human_check(report: CheckReport) -> str:
    lines: list[str] = []
    for result in sorted(report.results, key=lambda r: r.model_unique_id):
        receipt = result.receipt
        lines.append(f"model {receipt.model_unique_id} ({receipt.source_path.as_posix()})")
        lines.append(f"  status: {receipt.status.value} fixable={is_fixable(receipt)}")
        lines.append(f"  reasons: {', '.join(c.value for c in receipt.reason_codes)}")
        lines.append(
            f"  rows: baseline={receipt.equality.baseline_rows} candidate={receipt.equality.candidate_rows} "
            f"baseline_only={receipt.equality.baseline_only_occurrences} "
            f"candidate_only={receipt.equality.candidate_only_occurrences} "
            f"schema_equal={receipt.equality.schema_equal}"
        )
        lines.append(
            "  verification scope: one-statement snapshot-equivalent multiset comparison (not universal proof)"
        )
        if result.diff:
            lines.append(result.diff)
    return "\n".join(lines)
