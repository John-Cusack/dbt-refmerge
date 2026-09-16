"""Domain invariants: source spans, the fixable-receipt gate and typed errors."""

import dataclasses
from pathlib import Path

import pytest

from dbt_refmerge.domain import (
    EqualityResult,
    ReasonCode,
    SourceSpan,
    VerificationReceipt,
    VerificationStatus,
    is_fixable,
)
from dbt_refmerge.errors import (
    CleanupError,
    DbtError,
    InternalInvariantError,
    RefmergeError,
    ScratchBoundaryError,
    SourceParseError,
)

FIXABLE = VerificationReceipt(
    run_id="run-1",
    model_unique_id="model.p.orders",
    source_path=Path("models/orders.sql"),
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
    scratch_relations=(),
    equality=EqualityResult(
        schema_equal=True,
        baseline_rows=3,
        candidate_rows=3,
        baseline_only_occurrences=0,
        candidate_only_occurrences=0,
    ),
    status=VerificationStatus.SNAPSHOT_EQUIVALENT,
    reason_codes=(ReasonCode.OK,),
    warning_codes=(),
    cleanup_complete=True,
)


def test_is_fixable_accepts_a_clean_equivalent_receipt():
    assert is_fixable(FIXABLE) is True


@pytest.mark.parametrize(
    ("receipt_changes", "equality_changes"),
    [
        ({"status": VerificationStatus.NOT_RUN}, {}),
        ({"status": VerificationStatus.DIFFERENT}, {}),
        ({"status": VerificationStatus.UNVERIFIABLE}, {}),
        ({"status": VerificationStatus.ERROR}, {}),
        ({"status": "snapshot_equivalent"}, {}),
        ({"reason_codes": ()}, {}),
        ({"reason_codes": (ReasonCode.COMPILE_DRIFT,)}, {}),
        ({"reason_codes": (ReasonCode.OK, ReasonCode.OK)}, {}),
        ({"cleanup_complete": False}, {}),
        ({}, {"schema_equal": False}),
        ({}, {"candidate_rows": 4}),
        ({}, {"baseline_only_occurrences": 1}),
        ({}, {"candidate_only_occurrences": 1}),
    ],
    ids=[
        "not-run",
        "different",
        "unverifiable",
        "error",
        "status-is-a-plain-string",
        "no-reason-codes",
        "other-reason-code",
        "extra-reason-code",
        "cleanup-incomplete",
        "schema-differs",
        "row-counts-differ",
        "baseline-only-rows",
        "candidate-only-rows",
    ],
)
def test_is_fixable_requires_every_equivalence_condition(receipt_changes, equality_changes):
    receipt = dataclasses.replace(
        FIXABLE,
        equality=dataclasses.replace(FIXABLE.equality, **equality_changes),
        **receipt_changes,
    )

    assert is_fixable(receipt) is False


@pytest.mark.parametrize(("start", "end", "size"), [(0, 0, 0), (0, 10, 10), (3, 7, 10), (10, 10, 10)])
def test_source_span_validate_accepts_spans_within_the_file(start, end, size):
    assert SourceSpan(start, end).validate(size) is None


@pytest.mark.parametrize(
    ("start", "end", "size"),
    [(5, 1, 10), (-1, 2, 10), (0, 11, 10)],
    ids=["reversed", "negative-start", "past-end"],
)
def test_source_span_validate_rejects_spans_outside_the_file(start, end, size):
    with pytest.raises(InternalInvariantError) as exc_info:
        SourceSpan(start, end).validate(size)

    assert exc_info.value.reason_code is ReasonCode.INTERNAL_ERROR
    assert str(exc_info.value) == (
        f"[INTERNAL_ERROR] invalid source span SourceSpan(start_byte={start}, end_byte={end}) for size {size}"
    )


@pytest.mark.parametrize(
    ("error", "reason_code"),
    [
        (ScratchBoundaryError("boom"), ReasonCode.SCRATCH_BOUNDARY_VIOLATION),
        (CleanupError("boom"), ReasonCode.CLEANUP_FAILED),
        (InternalInvariantError("boom"), ReasonCode.INTERNAL_ERROR),
        (DbtError("boom"), ReasonCode.DBT_COMMAND_FAILED),
        (SourceParseError(ReasonCode.UNSUPPORTED_IMPORT_SHAPE, "boom"), ReasonCode.UNSUPPORTED_IMPORT_SHAPE),
    ],
    ids=lambda value: type(value).__name__ if isinstance(value, Exception) else value.value,
)
def test_typed_errors_carry_reason_code_and_message(error, reason_code):
    assert isinstance(error, RefmergeError)
    assert error.reason_code is reason_code
    assert error.message == "boom"
    assert str(error) == f"[{reason_code.value}] boom"


def test_dbt_error_keeps_explicit_reason_code_and_argv():
    error = DbtError("timed out", ReasonCode.WAREHOUSE_TIMEOUT, argv=("dbt", "run"))

    assert (error.reason_code, error.argv, str(error)) == (
        ReasonCode.WAREHOUSE_TIMEOUT,
        ("dbt", "run"),
        "[WAREHOUSE_TIMEOUT] timed out",
    )
    assert DbtError("boom").argv == ()
