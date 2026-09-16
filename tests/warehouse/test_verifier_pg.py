"""End to end on PostgreSQL with real dbt: check proves merges, refuses divergence, and cleans up."""

import dataclasses
import subprocess
from pathlib import Path

import pytest
from conftest import write_pg_profiles

from dbt_refmerge.config import AppConfig
from dbt_refmerge.domain import EqualityResult, ReasonCode, VerificationStatus, is_fixable
from dbt_refmerge.orchestrator import CheckRequest, CleanupRequest, FixRequest, RefmergeService
from dbt_refmerge.verification.runner import verify_postgres

pytestmark = pytest.mark.warehouse

STG_ORDERS = (
    "select * from (values (1, 10, 5.00), (2, 11, 7.50), (2, 11, 7.50), (3, null, null))"
    " as t(order_id, customer_id, amount)\n"
)
ORDERS = (
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
    "    select orders.order_id, orders.customer_id, order_financials.amount\n"
    "    from orders\n"
    "    join order_financials on orders.order_id = order_financials.order_id\n"
    ")\n"
    "select * from final\n"
)


@pytest.fixture
def warehouse_project(tmp_path, pg_dsn, pg_schemas, dbt_executable):
    """A dbt project whose upstream model is already built; returns (root, config)."""
    model_schema = pg_schemas("model")
    scratch = pg_schemas("scratch")
    root = tmp_path / "project"
    (root / "models").mkdir(parents=True)
    (root / "dbt_project.yml").write_text("name: it\nversion: 1.0.0\nconfig-version: 2\nprofile: it\n")
    (root / "models" / "stg_orders.sql").write_text(STG_ORDERS)
    (root / "models" / "orders.sql").write_text(ORDERS)
    profiles = write_pg_profiles(tmp_path / "profiles", pg_dsn, profile="it", schema=model_schema)
    subprocess.run(
        [dbt_executable, "run", "--select", "stg_orders", "--profiles-dir", str(profiles)],
        cwd=root,
        check=True,
        capture_output=True,
    )
    config = AppConfig(project_dir=root, profiles_dir=profiles, scratch_schema=scratch, dbt_command=(dbt_executable,))
    return root, config


def _relations_in(pg_dsn: str, schema: str) -> list[tuple[str, str]]:
    import psycopg2

    with psycopg2.connect(pg_dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "select table_name, table_type from information_schema.tables where table_schema = %s order by 1",
            (schema,),
        )
        return list(cur.fetchall())


def _orders_receipt(report):
    return next(r.receipt for r in report.results if r.model_unique_id == "model.it.orders")


def test_check_proves_merge_and_leaves_scratch_schema_empty(warehouse_project, pg_dsn):
    _root, config = warehouse_project

    report = RefmergeService().check(CheckRequest(config=config))

    receipt = _orders_receipt(report)
    assert (receipt.status, receipt.reason_codes) == (VerificationStatus.SNAPSHOT_EQUIVALENT, (ReasonCode.OK,))
    assert receipt.equality == EqualityResult(True, 6, 6, 0, 0)
    assert is_fixable(receipt)
    assert _relations_in(pg_dsn, config.scratch_schema) == []


def test_fix_applies_the_proven_merge(warehouse_project):
    root, config = warehouse_project

    report = RefmergeService().fix(FixRequest(config=config, model_path=Path("models/orders.sql")))

    assert (report.applied, report.reason) == (True, "applied")
    rewritten = (root / "models" / "orders.sql").read_text()
    assert "join orders as order_financials" in rewritten
    assert "order_financials as (" not in rewritten
    after = _orders_receipt(RefmergeService().check(CheckRequest(config=config)))
    assert after.reason_codes == (ReasonCode.NO_DUPLICATE_IMPORT,)


def _tampered(replace_from: str, replace_to: str):
    def runner(request):
        compiled = request.candidate.compiled_code or ""
        assert replace_from in compiled
        candidate = request.candidate.model_copy(update={"compiled_code": compiled.replace(replace_from, replace_to)})
        return verify_postgres(dataclasses.replace(request, candidate=candidate))

    return runner


@pytest.mark.parametrize(
    ("replace_to", "status", "codes", "equality"),
    [
        (
            "select * from final where order_id <> 3",
            VerificationStatus.DIFFERENT,
            (ReasonCode.BAG_DIFFERENCE,),
            EqualityResult(True, 6, 5, 1, 0),
        ),
        (
            "select * from final union all select * from final where order_id = 1",
            VerificationStatus.DIFFERENT,
            (ReasonCode.BAG_DIFFERENCE,),
            EqualityResult(True, 6, 7, 0, 1),
        ),
        (
            "select order_id, amount, customer_id from final",
            VerificationStatus.DIFFERENT,
            (ReasonCode.SCHEMA_MISMATCH,),
            EqualityResult(False, 0, 0, 0, 0),
        ),
        (
            "select order_id::bigint as order_id, customer_id, amount from final",
            VerificationStatus.DIFFERENT,
            (ReasonCode.SCHEMA_MISMATCH,),
            EqualityResult(False, 0, 0, 0, 0),
        ),
    ],
    ids=["fewer-rows", "extra-duplicate", "column-order", "column-type"],
)
def test_divergent_candidate_is_refused(warehouse_project, pg_dsn, replace_to, status, codes, equality):
    _root, config = warehouse_project

    report = RefmergeService(verify_runner=_tampered("select * from final", replace_to)).check(
        CheckRequest(config=config)
    )

    receipt = _orders_receipt(report)
    assert (receipt.status, receipt.reason_codes, receipt.equality) == (status, codes, equality)
    assert not is_fixable(receipt)
    assert _relations_in(pg_dsn, config.scratch_schema) == []


def test_unsupported_column_type_is_unverifiable(warehouse_project, pg_dsn):
    _root, config = warehouse_project
    runner = _tampered("select * from final", "select order_id, customer_id, amount::double precision from final")

    def both_sides(request):
        baseline = request.baseline.model_copy(
            update={
                "compiled_code": (request.baseline.compiled_code or "").replace(
                    "select * from final", "select order_id, customer_id, amount::double precision from final"
                )
            }
        )
        return runner(dataclasses.replace(request, baseline=baseline))

    receipt = _orders_receipt(RefmergeService(verify_runner=both_sides).check(CheckRequest(config=config)))

    assert (receipt.status, receipt.reason_codes) == (
        VerificationStatus.UNVERIFIABLE,
        (ReasonCode.UNSUPPORTED_COMPARISON_TYPE,),
    )
    assert _relations_in(pg_dsn, config.scratch_schema) == []


def test_cleanup_drops_leftover_views_for_the_run_only(warehouse_project, pg_dsn):
    import psycopg2

    _root, config = warehouse_project
    scratch = config.scratch_schema
    run_id = "20260916T120000_0123456789ab"
    ours = "dbt_refmerge_baseline_000_0123456789ab_deadbeef"
    ours_tmp = "dbt_refmerge_candidate_000_0123456789ab_deadbeef__dbt_tmp"
    other_run = "dbt_refmerge_baseline_000_ffffffffffff_deadbeef"
    table_with_our_name = "dbt_refmerge_candidate_000_0123456789ab_cafebabe"
    with psycopg2.connect(pg_dsn) as conn, conn.cursor() as cur:
        cur.execute(f'create schema if not exists "{scratch}"')
        for view in (ours, ours_tmp, other_run):
            cur.execute(f'create view "{scratch}"."{view}" as select 1 as x')
        cur.execute(f'create table "{scratch}"."{table_with_our_name}" (x int)')

    result = RefmergeService().cleanup(CleanupRequest(config=config, run_id=run_id))

    assert result["dropped"] == sorted([ours, ours_tmp]) and result["complete"]
    assert _relations_in(pg_dsn, scratch) == [(other_run, "VIEW"), (table_with_our_name, "BASE TABLE")]
