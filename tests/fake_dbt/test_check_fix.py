"""check / fix end to end against the fake dbt (no warehouse)."""

import dataclasses
from pathlib import Path

import pytest

from dbt_refmerge.config import AppConfig
from dbt_refmerge.dbt_cli import DbtCli
from dbt_refmerge.domain import EqualityResult, ReasonCode, VerificationStatus
from dbt_refmerge.orchestrator import CheckRequest, FixRequest, RefmergeService

pytestmark = pytest.mark.fake_dbt

README_MODEL = (
    "with orders as (\n"
    "    select\n"
    "        order_id,\n"
    "        customer_id\n"
    "    from {{ ref('stg_orders') }}\n"
    "),\n"
    "order_financials as (\n"
    "    select\n"
    "        order_id,\n"
    "        amount\n"
    "    from {{ ref('stg_orders') }}\n"
    "),\n"
    "final as (\n"
    "    select orders.order_id, order_financials.amount\n"
    "    from orders\n"
    "    join order_financials on orders.order_id = order_financials.order_id\n"
    ")\n"
    "select * from final\n"
)
STG = "select 1 as order_id, 2 as customer_id, 3 as amount\n"


def _config(root: Path, fake_dbt, tmp_path: Path) -> AppConfig:
    return AppConfig(
        project_dir=root,
        adapter="postgres",
        scratch_schema="refmerge_scratch",
        profiles_dir=tmp_path / "no-profiles",
        dbt_command=fake_dbt.command,
    )


def _results(report):
    return {r.model_unique_id: r for r in report.results}


def test_version_parses_dbt_core_output(fake_dbt, tmp_path):
    # B2: dbt >= 1.5 prints "Core:" on its own line.
    assert DbtCli(fake_dbt.command).version(cwd=tmp_path).version == "1.9.0"


def test_check_readme_example_reaches_verification(make_project, fake_dbt, tmp_path):
    # B2 + B3: without a verifier the merge must stop at INPUT_ISOLATION_UNAVAILABLE, not crash or drift.
    root = make_project({"models/stg_orders.sql": STG, "models/orders.sql": README_MODEL})

    report = RefmergeService().check(CheckRequest(config=_config(root, fake_dbt, tmp_path)))

    result = _results(report)["model.p.orders"]
    assert result.receipt.status is VerificationStatus.UNVERIFIABLE
    assert result.receipt.reason_codes == (ReasonCode.INPUT_ISOLATION_UNAVAILABLE,)
    assert "join orders as order_financials" in report.candidate_bytes_map["model.p.orders"].decode()


def test_check_continues_past_unparseable_model(make_project, fake_dbt, tmp_path):
    # B7: one model the source parser refuses must not abort the whole run.
    root = make_project(
        {
            "models/stg_orders.sql": STG,
            "models/orders.sql": README_MODEL,
            "models/recursive.sql": "with recursive r as (select 1 as n) select * from r\n",
        }
    )

    report = RefmergeService().check(CheckRequest(config=_config(root, fake_dbt, tmp_path)))

    results = _results(report)
    assert results["model.p.recursive"].receipt.status is VerificationStatus.UNVERIFIABLE
    assert results["model.p.recursive"].receipt.reason_codes == (ReasonCode.UNSUPPORTED_IMPORT_SHAPE,)
    assert results["model.p.orders"].receipt.reason_codes == (ReasonCode.INPUT_ISOLATION_UNAVAILABLE,)


def test_fix_applies_candidate_when_verifier_reports_equivalence(make_project, fake_dbt, tmp_path):
    root = make_project({"models/stg_orders.sql": STG, "models/orders.sql": README_MODEL})

    def verifier(ws, context, view, node, plan, candidate_bytes):
        from dbt_refmerge.orchestrator import _base_receipt

        receipt = _base_receipt(
            ws,
            context,
            view,
            node,
            node.raw_code.encode(),
            VerificationStatus.SNAPSHOT_EQUIVALENT,
            (ReasonCode.OK,),
            EqualityResult(True, 1, 1, 0, 0),
        )
        return dataclasses.replace(
            receipt,
            original_source_sha256=plan.original_source_sha256,
            candidate_source_sha256=plan.candidate_source_sha256,
        )

    report = RefmergeService(verify_runner=verifier).fix(
        FixRequest(config=_config(root, fake_dbt, tmp_path), model_path=Path("models/orders.sql"))
    )

    assert (report.applied, report.reason) == (True, "applied")
    assert "join orders as order_financials" in (root / "models" / "orders.sql").read_text()
