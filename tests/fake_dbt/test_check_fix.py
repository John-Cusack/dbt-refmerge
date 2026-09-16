"""check / fix end to end against the fake dbt (no warehouse)."""

from pathlib import Path

import pytest

from dbt_refmerge.config import AppConfig
from dbt_refmerge.dbt_cli import DbtCli
from dbt_refmerge.domain import ReasonCode, VerificationStatus, is_fixable
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


def test_check_readme_example_is_proven_equivalent(make_project, fake_dbt, tmp_path):
    # B1-B3: the README merge reaches the warehouse verifier and comes back fixable.
    root = make_project({"models/stg_orders.sql": STG, "models/orders.sql": README_MODEL})

    report = RefmergeService().check(CheckRequest(config=_config(root, fake_dbt, tmp_path)))

    receipt = _results(report)["model.p.orders"].receipt
    assert (receipt.status, receipt.reason_codes) == (VerificationStatus.SNAPSHOT_EQUIVALENT, (ReasonCode.OK,))
    assert is_fixable(receipt)
    assert [r.schema.value for r in receipt.scratch_relations] == ["refmerge_scratch", "refmerge_scratch"]
    assert "join orders as order_financials" in report.candidate_bytes_map["model.p.orders"].decode()
    commands = [call[0] for call in fake_dbt.calls()]
    assert commands.index("parse") < commands.index("run") < commands.index("run-operation")


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
    assert results["model.p.orders"].receipt.reason_codes == (ReasonCode.OK,)


def test_fix_applies_verified_candidate(make_project, fake_dbt, tmp_path):
    root = make_project({"models/stg_orders.sql": STG, "models/orders.sql": README_MODEL})

    report = RefmergeService().fix(
        FixRequest(config=_config(root, fake_dbt, tmp_path), model_path=Path("models/orders.sql"))
    )

    assert (report.applied, report.reason) == (True, "applied")
    assert "join orders as order_financials" in (root / "models" / "orders.sql").read_text()
    assert sorted(p.name for p in (root / "models").iterdir()) == ["orders.sql", "stg_orders.sql"]


def test_check_report_survives_local_workspace_cleanup_failure(make_project, fake_dbt, tmp_path, faults):
    root = make_project({"models/stg_orders.sql": STG, "models/orders.sql": README_MODEL})

    def refuse_rmdir(args):
        if "dbt_refmerge_" in str(args[0]):
            raise OSError("directory busy")

    faults.on("os.rmdir", refuse_rmdir)
    report = RefmergeService().check(CheckRequest(config=_config(root, fake_dbt, tmp_path)))

    assert {r.model_unique_id for r in report.results} == {"model.p.orders", "model.p.stg_orders"}


def test_unreadable_candidate_manifest_refuses_that_model_only(make_project, fake_dbt, tmp_path):
    fake_dbt.set_mode("candidate_bad_manifest")
    root = make_project({"models/stg_orders.sql": STG, "models/orders.sql": README_MODEL})

    report = RefmergeService().check(CheckRequest(config=_config(root, fake_dbt, tmp_path)))

    receipt = _results(report)["model.p.orders"].receipt
    assert (receipt.status, receipt.reason_codes) == (VerificationStatus.UNVERIFIABLE, (ReasonCode.INTERNAL_ERROR,))
