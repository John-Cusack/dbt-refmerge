"""Reporting: the --fail-on exit-code policy, versioned JSON documents and human rendering."""

import json
from pathlib import Path

import pytest

from dbt_refmerge.analyze import Finding, FindingStatus
from dbt_refmerge.config import FailOn
from dbt_refmerge.domain import (
    EqualityResult,
    IdentifierIdentity,
    ReasonCode,
    RelationIdentity,
    VerificationReceipt,
    VerificationStatus,
)
from dbt_refmerge.orchestrator import CheckReport, ModelResult, ScanReport
from dbt_refmerge.reporting import (
    ExitCode,
    check_report_json,
    evaluate_exit_code_for_check,
    render_human_check,
    scan_report_json,
)

EQUAL = EqualityResult(True, 3, 3, 0, 0)
NOTHING_COMPARED = EqualityResult(False, 0, 0, 0, 0)


def _receipt(
    uid: str,
    status: VerificationStatus,
    reason_codes: tuple[ReasonCode, ...],
    *,
    equality: EqualityResult = NOTHING_COMPARED,
    cleanup_complete: bool = True,
    scratch_relations: tuple[RelationIdentity, ...] = (),
    warning_codes: tuple[str, ...] = (),
) -> VerificationReceipt:
    return VerificationReceipt(
        run_id="run-1",
        model_unique_id=uid,
        source_path=Path("models") / f"{uid.rsplit('.', 1)[-1]}.sql",
        plan_sha256="plan",
        original_source_sha256="original",
        candidate_source_sha256="candidate",
        original_compiled_sha256="original-compiled",
        candidate_compiled_sha256="candidate-compiled",
        dbt_version="1.9.0",
        manifest_schema_version="v12",
        adapter_type="postgres",
        sqlglot_version="30.18.0",
        comparator_version="1",
        compilation_context_sha256="context",
        scratch_relations=scratch_relations,
        equality=equality,
        status=status,
        reason_codes=reason_codes,
        warning_codes=warning_codes,
        cleanup_complete=cleanup_complete,
    )


def _result(receipt: VerificationReceipt, diff: str = "") -> ModelResult:
    return ModelResult(receipt.model_unique_id, receipt.source_path, receipt, diff)


def _report(*results: ModelResult) -> CheckReport:
    return CheckReport(run_id="run-1", results=results, candidate_bytes_map={})


# One result of each kind the exit-code policy distinguishes.
KINDS = {
    "no_duplicate": (VerificationStatus.NOT_RUN, (ReasonCode.NO_DUPLICATE_IMPORT,)),
    "static_finding": (VerificationStatus.NOT_RUN, (ReasonCode.DIFFERENT_PREDICATE,)),
    "equivalent": (VerificationStatus.SNAPSHOT_EQUIVALENT, (ReasonCode.OK,)),
    "different": (VerificationStatus.DIFFERENT, (ReasonCode.BAG_DIFFERENCE,)),
    "unverifiable": (VerificationStatus.UNVERIFIABLE, (ReasonCode.INPUT_ISOLATION_UNAVAILABLE,)),
    "error": (VerificationStatus.ERROR, (ReasonCode.INTERNAL_ERROR,)),
}

FAIL_ON_COLUMNS = (FailOn.NEVER, FailOn.FINDING, FailOn.FIXABLE, FailOn.DIFFERENT, FailOn.UNVERIFIABLE)

# Results are ordered finding < fixable < different < unverifiable. `--fail-on X` fails the run when any
# result is at or above X and exits with the code of the most severe result; `never` never fails.
POLICY = [
    # results                                                  never finding fixable different unverifiable
    ((), (0, 0, 0, 0, 0)),
    (("no_duplicate",), (0, 0, 0, 0, 0)),
    (("static_finding",), (0, 2, 0, 0, 0)),
    (("equivalent",), (0, 2, 2, 0, 0)),
    (("different",), (0, 3, 3, 3, 0)),  # below the unverifiable threshold
    (("unverifiable",), (0, 4, 4, 4, 4)),
    (("error",), (0, 4, 4, 4, 4)),
    (("different", "unverifiable"), (0, 4, 4, 4, 4)),  # --fail-on different used to exit 0
    (("unverifiable", "different"), (0, 4, 4, 4, 4)),
    (("error", "different"), (0, 4, 4, 4, 4)),
    (("static_finding", "equivalent", "no_duplicate"), (0, 2, 2, 0, 0)),
    (("no_duplicate", "different", "static_finding", "equivalent"), (0, 3, 3, 3, 0)),
]


@pytest.mark.parametrize(
    ("kinds", "fail_on", "expected"),
    [
        pytest.param(kinds, fail_on, ExitCode(code), id=f"{'+'.join(kinds) or 'empty'}-{fail_on.value}")
        for kinds, codes in POLICY
        for fail_on, code in zip(FAIL_ON_COLUMNS, codes, strict=True)
    ],
)
def test_exit_code_policy_matrix(kinds, fail_on, expected):
    report = _report(*(_result(_receipt(f"model.p.m{i}", *KINDS[kind])) for i, kind in enumerate(kinds)))

    assert evaluate_exit_code_for_check(report, fail_on) is expected


def test_exit_code_values_are_stable():
    assert {code.name: int(code) for code in ExitCode} == {
        "OK": 0,
        "OPERATIONAL_ERROR": 1,
        "POLICY_FINDING": 2,
        "DIFFERENT": 3,
        "UNVERIFIABLE": 4,
        "INTERRUPTED": 130,
    }


def _relation(schema: str, identifier: str) -> RelationIdentity:
    return RelationIdentity(IdentifierIdentity("db"), IdentifierIdentity(schema), IdentifierIdentity(identifier))


def test_check_report_json_counts_each_status():
    fixable = _receipt(
        "model.p.a",
        VerificationStatus.SNAPSHOT_EQUIVALENT,
        (ReasonCode.OK,),
        equality=EQUAL,
        scratch_relations=(_relation("scratch", "baseline"), _relation("scratch", "candidate")),
        warning_codes=("W1",),
    )
    report = _report(
        _result(_receipt("model.p.e", VerificationStatus.ERROR, (ReasonCode.INTERNAL_ERROR,))),
        _result(_receipt("model.p.d", VerificationStatus.DIFFERENT, (ReasonCode.BAG_DIFFERENCE,))),
        _result(fixable, diff="--- a/models/a.sql\n+++ b/models/a.sql\n"),
        _result(_receipt("model.p.c", VerificationStatus.UNVERIFIABLE, (ReasonCode.COMPILE_DRIFT,))),
        _result(_receipt("model.p.b", VerificationStatus.NOT_RUN, (ReasonCode.NO_DUPLICATE_IMPORT,))),
        # Equivalent but not fixable: counted as a finding, not as fixable.
        _result(
            _receipt(
                "model.p.f",
                VerificationStatus.SNAPSHOT_EQUIVALENT,
                (ReasonCode.OK,),
                equality=EQUAL,
                cleanup_complete=False,
            )
        ),
    )

    doc = check_report_json(
        report,
        command="check",
        project_dir=Path("proj"),
        dbt_version="1.9.0",
        adapter_type="postgres",
        manifest_schema_version="v12",
    )

    assert json.loads(json.dumps(doc)) == doc
    assert {k: v for k, v in doc.items() if k != "models"} == {
        "schema_version": "1",
        "command": "check",
        "run_id": "run-1",
        "project_dir": str(Path("proj")),
        "dbt": {"version": "1.9.0", "adapter_type": "postgres", "manifest_schema_version": "v12"},
        "summary": {"models_scanned": 6, "findings": 2, "fixable": 1, "different": 1, "unverifiable": 1, "errors": 1},
        "cleanup": {"complete": False, "objects": []},
        "workspace": None,
    }
    assert [(m["model_unique_id"], m["status"], m["reason_codes"], m["fixable"]) for m in doc["models"]] == [
        ("model.p.a", "snapshot_equivalent", ["OK"], True),
        ("model.p.b", "not_run", ["NO_DUPLICATE_IMPORT"], False),
        ("model.p.c", "unverifiable", ["COMPILE_DRIFT"], False),
        ("model.p.d", "different", ["BAG_DIFFERENCE"], False),
        ("model.p.e", "error", ["INTERNAL_ERROR"], False),
        ("model.p.f", "snapshot_equivalent", ["OK"], False),
    ]
    assert doc["models"][0] == {
        "model_unique_id": "model.p.a",
        "source_path": "models/a.sql",
        "status": "snapshot_equivalent",
        "reason_codes": ["OK"],
        "warning_codes": ["W1"],
        "fixable": True,
        "equality": {
            "schema_equal": True,
            "baseline_rows": 3,
            "candidate_rows": 3,
            "baseline_only_occurrences": 0,
            "candidate_only_occurrences": 0,
        },
        "diff": "--- a/models/a.sql\n+++ b/models/a.sql\n",
        "scratch_relations": [
            {"database": "db", "schema": "scratch", "identifier": "baseline"},
            {"database": "db", "schema": "scratch", "identifier": "candidate"},
        ],
    }


def test_check_report_json_cleanup_complete_when_every_receipt_cleaned_up():
    report = _report(_result(_receipt("model.p.a", VerificationStatus.DIFFERENT, (ReasonCode.BAG_DIFFERENCE,))))

    doc = check_report_json(report, command="fix", project_dir=Path("proj"))

    assert doc["command"] == "fix"
    assert doc["cleanup"] == {"complete": True, "objects": []}


def test_scan_report_json_shape():
    report = ScanReport(
        findings=(
            Finding(
                model_unique_id="model.p.orders",
                source_path=Path("models/orders.sql"),
                upstream_unique_id="model.p.stg_orders",
                cte_names=("orders", "order_financials"),
                status=FindingStatus.MERGE_ELIGIBLE,
                reason_codes=(ReasonCode.OK,),
            ),
            Finding(
                model_unique_id="model.p.items",
                source_path=Path("models/items.sql"),
                upstream_unique_id="",
                cte_names=("a", "b"),
                status=FindingStatus.NOT_ELIGIBLE,
                reason_codes=(ReasonCode.DIFFERENT_PREDICATE, ReasonCode.PROJECTION_COLLISION),
            ),
        )
    )

    doc = scan_report_json(report, project_dir=Path("proj"))

    assert json.loads(json.dumps(doc)) == doc
    assert doc == {
        "schema_version": "1",
        "command": "scan",
        "project_dir": str(Path("proj")),
        "summary": {"findings": 2},
        "findings": [
            {
                "model_unique_id": "model.p.orders",
                "source_path": str(Path("models/orders.sql")),
                "upstream_unique_id": "model.p.stg_orders",
                "cte_names": ["orders", "order_financials"],
                "status": "merge_eligible",
                "reason_codes": ["OK"],
            },
            {
                "model_unique_id": "model.p.items",
                "source_path": str(Path("models/items.sql")),
                "upstream_unique_id": "",
                "cte_names": ["a", "b"],
                "status": "not_eligible",
                "reason_codes": ["DIFFERENT_PREDICATE", "PROJECTION_COLLISION"],
            },
        ],
    }


def test_scan_report_json_without_findings():
    assert scan_report_json(ScanReport(findings=()), project_dir=Path("proj"))["summary"] == {"findings": 0}


def test_render_human_check_includes_diff_only_when_present():
    diff = "--- a/models/b.sql\n+++ b/models/b.sql\n@@ -1 +1 @@\n-x\n+y\n"
    report = _report(
        _result(_receipt("model.p.b", VerificationStatus.SNAPSHOT_EQUIVALENT, (ReasonCode.OK,), equality=EQUAL), diff),
        _result(_receipt("model.p.a", VerificationStatus.UNVERIFIABLE, (ReasonCode.COMPILE_DRIFT, ReasonCode.OK))),
    )

    assert render_human_check(report) == "\n".join(
        [
            "model model.p.a (models/a.sql)",
            "  status: unverifiable fixable=False",
            "  reasons: COMPILE_DRIFT, OK",
            "  rows: baseline=0 candidate=0 baseline_only=0 candidate_only=0 schema_equal=False",
            "  verification scope: one-statement snapshot-equivalent multiset comparison (not universal proof)",
            "model model.p.b (models/b.sql)",
            "  status: snapshot_equivalent fixable=True",
            "  reasons: OK",
            "  rows: baseline=3 candidate=3 baseline_only=0 candidate_only=0 schema_equal=True",
            "  verification scope: one-statement snapshot-equivalent multiset comparison (not universal proof)",
            diff,
        ]
    )


def test_render_human_check_empty_report():
    assert render_human_check(_report()) == ""
