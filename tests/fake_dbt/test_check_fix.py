"""check / fix end to end against the fake dbt (no warehouse)."""

from pathlib import Path

import pytest

from dbt_refmerge.config import AppConfig, FailOn
from dbt_refmerge.dbt_cli import DbtCli
from dbt_refmerge.domain import ReasonCode, VerificationStatus, is_fixable
from dbt_refmerge.errors import RefmergeError
from dbt_refmerge.orchestrator import CheckRequest, FixRequest, RefmergeService
from dbt_refmerge.reporting import ExitCode, evaluate_exit_code_for_check

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
    recursive = "with recursive r as (select id from {{ ref('stg_orders') }}) select * from r join {{ ref('stg_orders') }} using (id)\n"
    root = make_project(
        {"models/stg_orders.sql": STG, "models/orders.sql": README_MODEL, "models/recursive.sql": recursive}
    )

    report = RefmergeService().check(CheckRequest(config=_config(root, fake_dbt, tmp_path)))

    results = _results(report)
    assert results["model.p.recursive"].receipt.status is VerificationStatus.UNVERIFIABLE
    assert results["model.p.recursive"].receipt.reason_codes == (ReasonCode.UNSUPPORTED_IMPORT_SHAPE,)
    assert results["model.p.orders"].receipt.reason_codes == (ReasonCode.OK,)


@pytest.mark.parametrize(
    "model",
    [
        "with recursive r as (select 1 as n) select * from r\n",
        "{{ config(materialized='incremental') }}\nselect * from {{ ref('stg_orders') }}\n",
        "{{ config(materialized='ephemeral') }}\nwith a as (select id from {{ ref('stg_orders') }}) select * from a\n",
    ],
    ids=["unparseable", "incremental", "ephemeral-single-import"],
)
def test_models_it_cannot_analyze_pass_when_they_import_nothing_twice(make_project, fake_dbt, tmp_path, model):
    # Refusing them would make every project with an incremental or recursive model fail check.
    root = make_project({"models/stg_orders.sql": STG, "models/other.sql": model})

    report = RefmergeService().check(CheckRequest(config=_config(root, fake_dbt, tmp_path)))

    receipt = _results(report)["model.p.other"].receipt
    assert (receipt.status, receipt.reason_codes) == (VerificationStatus.NOT_RUN, (ReasonCode.NO_DUPLICATE_IMPORT,))
    assert evaluate_exit_code_for_check(report, FailOn.FIXABLE) is ExitCode.OK


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


def _model(
    final: str = "select a.customer_id, b.amount from a join b using (id)", *, b_where: str = "", a_sep="\n"
) -> str:
    return (
        f"with a as (\n    select\n        id,{a_sep}        customer_id\n    from {{{{ ref('stg_orders') }}}}\n),\n"
        f"b as (\n    select\n        id,\n        amount\n    from {{{{ ref('stg_orders') }}}}{b_where}\n)\n"
        f"{final}\n"
    )


@pytest.mark.parametrize(
    ("model", "mode", "status", "codes"),
    [
        (
            "{{ config(materialized='incremental') }}\n" + _model(),
            None,
            VerificationStatus.UNVERIFIABLE,
            (ReasonCode.UNSUPPORTED_MODEL_TYPE,),
        ),
        (
            _model()
            .replace("        customer_id\n", "        customer_id\n", 1)
            .replace("    select\n        id,\n        amount", "    select distinct\n        id,\n        amount"),
            None,
            VerificationStatus.UNVERIFIABLE,
            (ReasonCode.UNSUPPORTED_IMPORT_SHAPE,),
        ),
        (
            _model("select a.customer_id, b.amount, '{{ var(\"x\") }}' from a join b using (id)"),
            None,
            VerificationStatus.UNVERIFIABLE,
            (ReasonCode.HARNESS_EMBEDDING_UNSAFE,),
        ),
        (
            _model(b_where=" where amount > 0"),
            None,
            VerificationStatus.UNVERIFIABLE,
            (ReasonCode.DIFFERENT_PREDICATE,),
        ),
        (
            _model(a_sep=" "),
            None,
            VerificationStatus.UNVERIFIABLE,
            (ReasonCode.UNSUPPORTED_IMPORT_SHAPE,),
        ),
        (_model(), "candidate_compile_fail", VerificationStatus.UNVERIFIABLE, (ReasonCode.DBT_COMMAND_FAILED,)),
        (_model(), "candidate_drop_node", VerificationStatus.UNVERIFIABLE, (ReasonCode.COMPILE_DRIFT,)),
        (_model(), "candidate_drift", VerificationStatus.UNVERIFIABLE, (ReasonCode.COMPILE_DRIFT,)),
        (
            _model(),
            "original_file_path_prefix=moved/",
            VerificationStatus.UNVERIFIABLE,
            (ReasonCode.SOURCE_MAPPING_AMBIGUOUS,),
        ),
        (
            # SQL spelling a ref sentinel is refused, and the duplicate imports still make that a finding.
            _model("select a.customer_id as __r0__, b.amount from a join b using (id)"),
            None,
            VerificationStatus.UNVERIFIABLE,
            (ReasonCode.UNSUPPORTED_IMPORT_SHAPE,),
        ),
    ],
    ids=[
        "incremental",
        "unsupported-duplicate-import",
        "unrendered-jinja",
        "different-predicates",
        "single-line-select-list",
        "candidate-compile-fails",
        "candidate-node-missing",
        "candidate-drift",
        "source-file-not-in-snapshot",
        "sentinel-name-in-sql",
    ],
)
def test_check_refuses_models_it_cannot_prove(make_project, fake_dbt, tmp_path, model, mode, status, codes):
    if mode:
        fake_dbt.set_mode(mode)
    root = make_project({"models/stg_orders.sql": STG, "models/m.sql": model})

    report = RefmergeService().check(CheckRequest(config=_config(root, fake_dbt, tmp_path)))

    receipt = _results(report)["model.p.m"].receipt
    assert (receipt.status, receipt.reason_codes) == (status, codes)
    assert not is_fixable(receipt)
    assert "run" not in [call[0] for call in fake_dbt.calls()]


def test_check_never_falls_back_to_a_manifest_outside_its_workspace(make_project, fake_dbt, tmp_path):
    fake_dbt.set_mode("ignore_target_path")
    root = make_project({"models/stg_orders.sql": STG, "models/orders.sql": README_MODEL})

    with pytest.raises(RefmergeError, match="cannot read manifest"):
        RefmergeService().check(CheckRequest(config=_config(root, fake_dbt, tmp_path)))


@pytest.mark.parametrize(
    ("mode", "message"),
    [("version_fail", "dbt --version failed"), ("compile_fail", "dbt compile failed")],
)
def test_check_aborts_when_dbt_itself_fails(make_project, fake_dbt, tmp_path, mode, message):
    fake_dbt.set_mode(mode)
    root = make_project({"models/stg_orders.sql": STG, "models/orders.sql": README_MODEL})

    with pytest.raises(RefmergeError, match=message):
        RefmergeService().check(CheckRequest(config=_config(root, fake_dbt, tmp_path)))


def test_check_with_a_selector_that_matches_nothing_is_an_error(make_project, fake_dbt, tmp_path):
    root = make_project({"models/stg_orders.sql": STG, "models/orders.sql": README_MODEL})

    with pytest.raises(RefmergeError, match="no models selected by 'nomatch'"):
        RefmergeService().check(CheckRequest(config=_config(root, fake_dbt, tmp_path), select="nomatch"))


def test_manifest_adapter_mismatch_aborts(make_project, fake_dbt, tmp_path):
    fake_dbt.set_mode("adapter_type=snowflake")
    root = make_project({"models/stg_orders.sql": STG, "models/orders.sql": README_MODEL})

    with pytest.raises(RefmergeError) as exc_info:
        RefmergeService().check(CheckRequest(config=_config(root, fake_dbt, tmp_path)))
    assert exc_info.value.reason_code is ReasonCode.ADAPTER_MISMATCH


def test_fix_path_outside_the_project_is_model_not_found(make_project, fake_dbt, tmp_path):
    root = make_project({"models/stg_orders.sql": STG, "models/orders.sql": README_MODEL})
    outside = tmp_path / "elsewhere.sql"
    outside.write_text(README_MODEL)

    report = RefmergeService().fix(FixRequest(config=_config(root, fake_dbt, tmp_path), model_path=outside))

    assert (report.applied, report.result, report.reason) == (False, None, "model not found")
    assert fake_dbt.calls() == []
