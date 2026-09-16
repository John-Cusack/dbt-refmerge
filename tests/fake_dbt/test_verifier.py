"""Warehouse verifier control flow against the fake dbt: every refusal, failure and cleanup path."""

import dataclasses
import json
from pathlib import Path

import pytest

from dbt_refmerge.adapters import get_spec
from dbt_refmerge.config import AppConfig
from dbt_refmerge.domain import ReasonCode, VerificationStatus, is_fixable
from dbt_refmerge.errors import RefmergeError
from dbt_refmerge.orchestrator import CheckRequest, CleanupRequest, FixRequest, RefmergeService
from dbt_refmerge.verification.runner import verify_postgres

pytestmark = pytest.mark.fake_dbt

MODEL = (
    "with a as (\n"
    "    select\n"
    "        id,\n"
    "        customer_id\n"
    "    from {{ ref('stg') }}\n"
    "),\n"
    "b as (\n"
    "    select\n"
    "        id,\n"
    "        amount\n"
    "    from {{ ref('stg') }}\n"
    ")\n"
    "select a.customer_id, b.amount from a join b using (id)\n"
)
FILES = {"models/stg.sql": "select 1 as id, 2 as customer_id, 3 as amount\n", "models/m.sql": MODEL}


def _config(root: Path, fake_dbt, tmp_path: Path, scratch_schema: str = "refmerge_scratch") -> AppConfig:
    return AppConfig(
        project_dir=root,
        adapter="postgres",
        scratch_schema=scratch_schema,
        profiles_dir=tmp_path / "no-profiles",
        dbt_command=fake_dbt.command,
    )


def _check(make_project, fake_dbt, tmp_path, **config_kwargs):
    root = make_project(FILES)
    report = RefmergeService().check(CheckRequest(config=_config(root, fake_dbt, tmp_path, **config_kwargs)))
    return next(r.receipt for r in report.results if r.model_unique_id == "model.p.m")


def _operations(fake_dbt) -> list[str]:
    return [call[1] for call in fake_dbt.calls() if call[0] == "run-operation"]


def _query_labels(fake_dbt) -> list[str]:
    return [
        json.loads(call[call.index("--args") + 1])["label"]
        for call in fake_dbt.calls()
        if call[:2] == ["run-operation", "dbt_refmerge_query"]
    ]


def test_equivalent_views_are_fixable_and_dropped(make_project, fake_dbt, tmp_path):
    receipt = _check(make_project, fake_dbt, tmp_path)

    assert (receipt.status, receipt.reason_codes, receipt.cleanup_complete) == (
        VerificationStatus.SNAPSHOT_EQUIVALENT,
        (ReasonCode.OK,),
        True,
    )
    assert is_fixable(receipt)
    assert _query_labels(fake_dbt) == ["schema", "verdict"]
    drop = next(c for c in fake_dbt.calls() if c[:2] == ["run-operation", "dbt_refmerge_drop_views"])
    drop_args = json.loads(drop[drop.index("--args") + 1])
    assert drop_args["schema"] == "refmerge_scratch"
    assert {r.identifier.value for r in receipt.scratch_relations} <= set(drop_args["identifiers"])
    parse = next(c for c in fake_dbt.calls() if c[0] == "parse")
    assert json.loads(parse[parse.index("--vars") + 1]) == {"dbt_refmerge_scratch_schema": "refmerge_scratch"}


def test_schema_difference_is_different(make_project, fake_dbt, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_DBT_CANDIDATE_COLUMNS", '[["id", "bigint"]]')

    receipt = _check(make_project, fake_dbt, tmp_path)

    assert (receipt.status, receipt.reason_codes) == (VerificationStatus.DIFFERENT, (ReasonCode.SCHEMA_MISMATCH,))
    assert "verdict" not in _query_labels(fake_dbt)
    assert receipt.cleanup_complete and not is_fixable(receipt)


def test_unsupported_column_type_is_unverifiable(make_project, fake_dbt, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_DBT_BASELINE_COLUMNS", '[["payload", "jsonb"]]')
    monkeypatch.setenv("FAKE_DBT_CANDIDATE_COLUMNS", '[["payload", "jsonb"]]')

    receipt = _check(make_project, fake_dbt, tmp_path)

    assert (receipt.status, receipt.reason_codes) == (
        VerificationStatus.UNVERIFIABLE,
        (ReasonCode.UNSUPPORTED_COMPARISON_TYPE,),
    )


def test_row_difference_is_different_with_counts(make_project, fake_dbt, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_DBT_VERDICT", "3,2,1,0")

    receipt = _check(make_project, fake_dbt, tmp_path)

    assert (receipt.status, receipt.reason_codes) == (VerificationStatus.DIFFERENT, (ReasonCode.BAG_DIFFERENCE,))
    equality = receipt.equality
    assert (equality.baseline_rows, equality.candidate_rows) == (3, 2)
    assert (equality.baseline_only_occurrences, equality.candidate_only_occurrences) == (1, 0)


@pytest.mark.parametrize(
    "mode",
    ["harness_materialized=table", "harness_schema=public", "harness_extra_node"],
    ids=["table", "outside-scratch", "extra-writable-node"],
)
def test_preflight_violation_refuses_before_creating_anything(make_project, fake_dbt, tmp_path, mode):
    fake_dbt.set_mode(mode)

    receipt = _check(make_project, fake_dbt, tmp_path)

    assert (receipt.status, receipt.reason_codes) == (
        VerificationStatus.UNVERIFIABLE,
        (ReasonCode.SCRATCH_BOUNDARY_VIOLATION,),
    )
    assert "run" not in [call[0] for call in fake_dbt.calls()]
    assert _operations(fake_dbt) == [] and receipt.scratch_relations == ()


def test_scratch_schema_equal_to_model_schema_refuses_before_dbt(make_project, fake_dbt, tmp_path):
    receipt = _check(make_project, fake_dbt, tmp_path, scratch_schema="sch")

    assert receipt.reason_codes == (ReasonCode.SCRATCH_BOUNDARY_VIOLATION,)
    assert "parse" not in [call[0] for call in fake_dbt.calls()]


@pytest.mark.parametrize(
    ("mode", "expected_ops"),
    [
        ("parse_fail", []),
        ("run_fail", ["dbt_refmerge_drop_views"]),
        ("query_fail=verdict", ["dbt_refmerge_query", "dbt_refmerge_query", "dbt_refmerge_drop_views"]),
    ],
)
def test_dbt_failures_are_errors_and_created_views_are_still_dropped(
    make_project, fake_dbt, tmp_path, mode, expected_ops
):
    fake_dbt.set_mode(mode)

    receipt = _check(make_project, fake_dbt, tmp_path)

    assert (receipt.status, receipt.reason_codes) == (VerificationStatus.ERROR, (ReasonCode.DBT_COMMAND_FAILED,))
    assert _operations(fake_dbt) == expected_ops
    assert receipt.cleanup_complete


@pytest.mark.parametrize("mode", ["drop_fail", "remaining"])
def test_incomplete_cleanup_blocks_fix(make_project, fake_dbt, tmp_path, monkeypatch, mode):
    if mode == "remaining":
        monkeypatch.setenv("FAKE_DBT_REMAINING", "all")
    else:
        fake_dbt.set_mode(mode)
    root = make_project(FILES)

    report = RefmergeService().fix(
        FixRequest(config=_config(root, fake_dbt, tmp_path), model_path=Path("models/m.sql"))
    )

    assert report.result is not None
    receipt = report.result.receipt
    assert receipt.status is VerificationStatus.SNAPSHOT_EQUIVALENT and not receipt.cleanup_complete
    assert (report.applied, report.reason) == (False, "not fixable")
    assert (root / "models" / "m.sql").read_text() == MODEL


@pytest.mark.parametrize(
    "output",
    [
        "no markers at all",
        "DBT_REFMERGE_RESULT_{nonce}_BEGIN\n{not json\nDBT_REFMERGE_RESULT_{nonce}_END\n",
        'DBT_REFMERGE_RESULT_{nonce}_BEGIN\n{"columns": ["a"]}\nDBT_REFMERGE_RESULT_{nonce}_END\n',
        'DBT_REFMERGE_RESULT_{nonce}_BEGIN\n{"columns": ["a"], "rows": [[1]]}\nDBT_REFMERGE_RESULT_{nonce}_END\n',
    ],
    ids=["no-markers", "bad-json", "no-rows", "non-string-value"],
)
def test_malformed_query_output_is_refused(make_project, fake_dbt, tmp_path, monkeypatch, output):
    monkeypatch.setenv("FAKE_DBT_QUERY_OUTPUT", output)

    receipt = _check(make_project, fake_dbt, tmp_path)

    assert receipt.status is VerificationStatus.UNVERIFIABLE
    assert receipt.reason_codes == (ReasonCode.DBT_COMMAND_FAILED,)
    assert not is_fixable(receipt)


def test_cleanup_drops_only_views_named_for_the_run(make_project, fake_dbt, tmp_path, monkeypatch):
    run_id = "20260916T120000_0123456789ab"
    # The run token is the last 16 characters of the run id.
    ours = "dbt_refmerge_baseline_000_0123456789ab_deadbeef"
    ours_tmp = "dbt_refmerge_candidate_000_0123456789ab_deadbeef__dbt_tmp"
    other_run = "dbt_refmerge_baseline_000_ffffffffffff_deadbeef"
    monkeypatch.setenv("FAKE_DBT_RUN_VIEWS", ",".join([ours, ours_tmp, other_run, "customers"]))
    root = make_project(FILES)

    result = RefmergeService().cleanup(CleanupRequest(config=_config(root, fake_dbt, tmp_path), run_id=run_id))

    assert result == {
        "run_id": run_id,
        "schema": "refmerge_scratch",
        "dropped": sorted([ours, ours_tmp]),
        "remaining": [],
        "complete": True,
    }
    drop = next(c for c in fake_dbt.calls() if c[:2] == ["run-operation", "dbt_refmerge_drop_views"])
    assert json.loads(drop[drop.index("--args") + 1])["identifiers"] == sorted([ours, ours_tmp])


def test_cleanup_rejects_malformed_run_id(make_project, fake_dbt, tmp_path):
    root = make_project(FILES)
    with pytest.raises(RefmergeError) as exc_info:
        RefmergeService().cleanup(CleanupRequest(config=_config(root, fake_dbt, tmp_path), run_id="x'; drop table y"))
    assert exc_info.value.reason_code is ReasonCode.CLEANUP_FAILED
    assert fake_dbt.calls() == []


@pytest.mark.parametrize(
    ("tamper", "code"),
    [
        (
            lambda r: dataclasses.replace(r, baseline=r.baseline.model_copy(update={"database": None})),
            ReasonCode.SCRATCH_BOUNDARY_VIOLATION,
        ),
        (lambda r: dataclasses.replace(r, spec=get_spec("snowflake")), ReasonCode.UNSUPPORTED_ADAPTER),
    ],
    ids=["no-database", "adapter-cannot-verify"],
)
def test_verifier_refuses_requests_it_cannot_scope(make_project, fake_dbt, tmp_path, tamper, code):
    root = make_project(FILES)
    service = RefmergeService(verify_runner=lambda request: verify_postgres(tamper(request)))

    report = service.check(CheckRequest(config=_config(root, fake_dbt, tmp_path)))

    receipt = next(r.receipt for r in report.results if r.model_unique_id == "model.p.m")
    assert (receipt.status, receipt.reason_codes) == (VerificationStatus.UNVERIFIABLE, (code,))
    assert "parse" not in [call[0] for call in fake_dbt.calls()]


def test_cleanup_with_nothing_left_drops_nothing(make_project, fake_dbt, tmp_path):
    root = make_project(FILES)

    result = RefmergeService().cleanup(
        CleanupRequest(config=_config(root, fake_dbt, tmp_path), run_id="20260916T120000_0123456789ab")
    )

    assert (result["dropped"], result["remaining"], result["complete"]) == ([], [], True)
    assert _operations(fake_dbt) == ["dbt_refmerge_query"]
