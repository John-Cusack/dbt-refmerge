"""Reporting: human rendering, versioned JSON, exit-code policy."""

from __future__ import annotations

from enum import IntEnum
from pathlib import Path
from typing import Any

from dbt_refmerge.config import FailOn
from dbt_refmerge.domain import ReasonCode, VerificationStatus, is_fixable
from dbt_refmerge.orchestrator import CheckReport, ScanReport


class ExitCode(IntEnum):
    OK = 0
    OPERATIONAL_ERROR = 1
    POLICY_FINDING = 2
    DIFFERENT = 3
    UNVERIFIABLE = 4
    INTERRUPTED = 130


# Explicit severity table (never derived from enum ordering).
_SEVERITY = {
    ExitCode.OK: 0,
    ExitCode.POLICY_FINDING: 1,
    ExitCode.DIFFERENT: 2,
    ExitCode.UNVERIFIABLE: 3,
    ExitCode.OPERATIONAL_ERROR: 4,
}

JSON_SCHEMA_VERSION = "1"


def evaluate_exit_code_for_check(report: CheckReport, fail_on: FailOn) -> ExitCode:
    if fail_on == FailOn.NEVER:
        return ExitCode.OK
    worst = ExitCode.OK
    for result in report.results:
        receipt = result.receipt
        code = ExitCode.OK
        if receipt.status == VerificationStatus.DIFFERENT:
            code = ExitCode.DIFFERENT
        elif receipt.status in (VerificationStatus.UNVERIFIABLE, VerificationStatus.ERROR):
            code = ExitCode.UNVERIFIABLE
        elif receipt.status == VerificationStatus.SNAPSHOT_EQUIVALENT:
            if fail_on in (FailOn.FINDING, FailOn.FIXABLE, FailOn.DIFFERENT, FailOn.UNVERIFIABLE):
                # fixable finding triggers policy when fail_on <= FIXABLE
                code = ExitCode.POLICY_FINDING if fail_on in (FailOn.FINDING, FailOn.FIXABLE) else ExitCode.OK
            else:
                code = ExitCode.OK
        elif receipt.status == VerificationStatus.NOT_RUN:
            has_finding = receipt.reason_codes != (ReasonCode.NO_DUPLICATE_IMPORT,)
            if has_finding and fail_on == FailOn.FINDING:
                code = ExitCode.POLICY_FINDING
            else:
                code = ExitCode.OK
        # fail_on gates
        if fail_on == FailOn.FIXABLE and receipt.status == VerificationStatus.DIFFERENT:
            code = ExitCode.DIFFERENT  # still surfaces
        if _SEVERITY[code] > _SEVERITY[worst]:
            # apply fail_on filter: DIFFERENT only fails when fail_on == DIFFERENT or stricter?
            # Spec: fail_on controls policy findings without changing operational-error meanings.
            # DIFFERENT always surfaces as 3; UNVERIFIABLE as 4 when policy requires verification.
            worst = code
    # filter by fail_on level
    if fail_on == FailOn.FINDING:
        return worst
    if fail_on == FailOn.FIXABLE:
        if worst == ExitCode.POLICY_FINDING:
            return worst
        # DIFFERENT/UNVERIFIABLE still reported
        return worst if worst in (ExitCode.DIFFERENT, ExitCode.UNVERIFIABLE) else ExitCode.OK
    if fail_on == FailOn.DIFFERENT:
        return worst if worst == ExitCode.DIFFERENT else ExitCode.OK
    if fail_on == FailOn.UNVERIFIABLE:
        return worst if worst == ExitCode.UNVERIFIABLE else ExitCode.OK
    return worst


def check_report_json(
    report: CheckReport,
    *,
    command: str,
    project_dir: Path,
    dbt_version: str = "",
    adapter_type: str = "postgres",
    manifest_schema_version: str = "",
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
                "source_path": str(receipt.source_path),
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
        "cleanup": {"complete": True, "objects": []},
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
            }
            for f in report.findings
        ],
    }


def render_human_check(report: CheckReport) -> str:
    lines: list[str] = []
    for result in sorted(report.results, key=lambda r: r.model_unique_id):
        receipt = result.receipt
        lines.append(f"model {receipt.model_unique_id} ({receipt.source_path})")
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
