"""Pruning and pruning-plus-merging through real dbt subprocess seams."""

from pathlib import Path

import pytest

from dbt_refmerge.config import AppConfig
from dbt_refmerge.domain import ReasonCode, VerificationStatus, is_fixable
from dbt_refmerge.orchestrator import CheckRequest, FixRequest, RefmergeService, ScanRequest

pytestmark = pytest.mark.fake_dbt

STG = "select 1 as id, 10 as account_id, 5::numeric as amount, true as active, 'x' as unused\n"
REF = "{{ ref('stg') }}"


def _config(root, fake_dbt):
    return AppConfig(
        project_dir=root, adapter="postgres", scratch_schema="refmerge_scratch", dbt_command=fake_dbt.command
    )


@pytest.mark.parametrize("body", ["*", "id, unused", "t.* from-alias"])
def test_fix_narrows_single_import_and_a_second_check_is_a_noop(make_project, fake_dbt, body):
    if body == "t.* from-alias":
        raw = f"with a as (select t.* from {REF} as t) select id from a\n"
        expected = raw.replace("select t.*", "select id")
    else:
        raw = f"with a as (select {body} from {REF}) select id from a\n"
        expected = raw.replace(f"select {body}", "select id")
    root = make_project({"models/stg.sql": STG, "models/m.sql": raw})
    config = _config(root, fake_dbt)
    service = RefmergeService()
    report = service.fix(FixRequest(config, Path("models/m.sql")))
    assert report.applied
    assert is_fixable(report.result.receipt)
    assert report.result.receipt.cleanup_complete
    assert (root / "models/m.sql").read_bytes() == expected.encode()
    assert service.scan(ScanRequest(config)).findings == ()
    receipt = service.check(CheckRequest(config, model_path=Path("models/m.sql"))).results[0].receipt
    assert (receipt.status, receipt.reason_codes) == (VerificationStatus.NOT_RUN, (ReasonCode.NO_DUPLICATE_IMPORT,))


def test_narrowed_stars_merge_in_the_same_check(make_project, fake_dbt):
    raw = (
        f"with a as (select * from {REF}),\n"
        f"b as (select * from {REF})\n"
        "select a.id, b.amount from a join b on a.id = b.id\n"
    )
    root = make_project({"models/stg.sql": STG, "models/m.sql": raw})
    config = _config(root, fake_dbt)
    report = RefmergeService().check(CheckRequest(config, model_path=Path("models/m.sql")))
    assert is_fixable(report.results[0].receipt)
    assert (
        report.candidate_bytes_map["model.p.m"]
        == (
            f"with a as (select id, amount from {REF})\nselect a.id, b.amount from a join a as b on a.id = b.id\n"
        ).encode()
    )
    assert (root / "models/m.sql").read_bytes() == raw.encode()


def test_different_predicates_allow_pruning_without_merging(make_project, fake_dbt):
    raw = (
        f"with a as (select * from {REF} where active),\n"
        f"b as (select * from {REF} where amount > 0)\n"
        "select a.id, b.amount from a join b on a.id = b.id\n"
    )
    root = make_project({"models/stg.sql": STG, "models/m.sql": raw})
    report = RefmergeService().check(CheckRequest(_config(root, fake_dbt), model_path=Path("models/m.sql")))
    assert is_fixable(report.results[0].receipt)
    candidate = report.candidate_bytes_map["model.p.m"].decode()
    assert "b as (" in candidate
    assert "where active" in candidate
    assert "where amount > 0" in candidate
    assert "select *" not in candidate


@pytest.mark.parametrize(
    ("mode", "code"),
    [("candidate_drift", ReasonCode.COMPILE_DRIFT), ("candidate_compile_fail", ReasonCode.DBT_COMMAND_FAILED)],
)
def test_pruning_refuses_compiled_drift_and_compile_failure(make_project, fake_dbt, mode, code):
    fake_dbt.set_mode(mode)
    raw = f"with a as (select * from {REF}) select id from a\n"
    root = make_project({"models/stg.sql": STG, "models/m.sql": raw})
    report = RefmergeService().fix(FixRequest(_config(root, fake_dbt), Path("models/m.sql")))
    assert not report.applied
    assert report.result.receipt.reason_codes == (code,)
    assert "run" not in [call[0] for call in fake_dbt.calls()]
    assert (root / "models/m.sql").read_bytes() == raw.encode()


def test_nondeterministic_single_import_pruning_is_reported_as_a_refusal(make_project, fake_dbt):
    raw = f"with a as (select * from {REF}) select id, random() from a\n"
    root = make_project({"models/stg.sql": STG, "models/m.sql": raw})
    report = RefmergeService().check(CheckRequest(_config(root, fake_dbt), model_path=Path("models/m.sql")))
    assert (report.results[0].receipt.status, report.results[0].receipt.reason_codes) == (
        VerificationStatus.UNVERIFIABLE,
        (ReasonCode.NONDETERMINISTIC,),
    )


def test_fix_dry_run_preserves_source(make_project, fake_dbt):
    raw = f"with a as (select * from {REF}) select id from a\n"
    root = make_project({"models/stg.sql": STG, "models/m.sql": raw})
    report = RefmergeService().fix(FixRequest(_config(root, fake_dbt), Path("models/m.sql"), dry_run=True))
    assert not report.applied and report.dry_run
    assert is_fixable(report.result.receipt)
    assert "-with a as (select *" in report.result.diff
    assert "+with a as (select id" in report.result.diff
    assert (root / "models/m.sql").read_bytes() == raw.encode()


def test_pruning_retains_an_unrelated_first_cte(make_project, fake_dbt):
    raw = f"with seed as (select 1 as n), a as (select * from {REF}) select id from a\n"
    root = make_project({"models/stg.sql": STG, "models/m.sql": raw})
    report = RefmergeService().check(CheckRequest(_config(root, fake_dbt), model_path=Path("models/m.sql")))
    assert is_fixable(report.results[0].receipt)
    assert report.candidate_bytes_map["model.p.m"] == raw.replace("select *", "select id").encode()


def test_merge_keeps_existing_comment_relocation_refusal(make_project, fake_dbt):
    raw = (
        f"with a as (select id, account_id -- keep me\nfrom {REF}),\n"
        f"b as (select id, amount from {REF})\n"
        "select a.account_id, b.amount from a join b using (id)\n"
    )
    root = make_project({"models/stg.sql": STG, "models/m.sql": raw})
    report = RefmergeService().check(CheckRequest(_config(root, fake_dbt), model_path=Path("models/m.sql")))
    assert report.results[0].receipt.reason_codes == (ReasonCode.COMMENT_RELOCATION_UNSUPPORTED,)
    assert "run" not in [call[0] for call in fake_dbt.calls()]
