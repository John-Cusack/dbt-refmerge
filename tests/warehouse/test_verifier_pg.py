"""End to end on PostgreSQL with real dbt: check proves merges, refuses divergence, and cleans up."""

import dataclasses
import shutil
import subprocess
import types
import uuid
from pathlib import Path

import pytest
from conftest import drop_pg_schemas, find_dbt, require_pg_dsn, write_pg_profiles

from dbt_refmerge.config import AppConfig
from dbt_refmerge.domain import EqualityResult, ReasonCode, VerificationStatus, is_fixable
from dbt_refmerge.orchestrator import CheckRequest, CleanupRequest, FixRequest, RefmergeService, ScanRequest
from dbt_refmerge.verification.runner import verify_postgres, verify_postgres_batch

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


def _build_project(root: Path, profiles_dir: Path, dsn: str, dbt: str, model_schema: str) -> Path:
    (root / "models").mkdir(parents=True)
    (root / "dbt_project.yml").write_text("name: it\nversion: 1.0.0\nconfig-version: 2\nprofile: it\n")
    (root / "models" / "stg_orders.sql").write_text(STG_ORDERS)
    (root / "models" / "orders.sql").write_text(ORDERS)
    profiles = write_pg_profiles(profiles_dir, dsn, profile="it", schema=model_schema)
    subprocess.run(
        [dbt, "run", "--select", "stg_orders", "--profiles-dir", str(profiles)],
        cwd=root,
        check=True,
        capture_output=True,
    )
    return profiles


@pytest.fixture(scope="module")
def proven(tmp_path_factory):
    """One built project and one real check, shared by the read-only tests.

    The check keeps its workspace and records the verifier's request for the orders model, so the
    divergence tests can re-run just the warehouse verification with a tampered candidate instead of
    repeating the snapshot and both compiles.
    """
    dsn, dbt = require_pg_dsn(), find_dbt()
    token = uuid.uuid4().hex[:8]
    model_schema, scratch = f"refmerge_it_model_{token}", f"refmerge_it_scratch_{token}"
    tmp = tmp_path_factory.mktemp("proven")
    try:
        profiles = _build_project(tmp / "project", tmp / "profiles", dsn, dbt, model_schema)
        config = AppConfig(
            project_dir=tmp / "project",
            profiles_dir=profiles,
            scratch_schema=scratch,
            dbt_command=(dbt,),
            keep_workspace=True,
        )
        requests = []

        def recording(batch):
            requests.extend(batch)
            return verify_postgres_batch(batch)

        report = RefmergeService(verify_runner=recording).check(CheckRequest(config=config))
        request = next(r for r in requests if r.baseline.unique_id == "model.it.orders")
        yield types.SimpleNamespace(dsn=dsn, config=config, report=report, request=request)
    finally:
        drop_pg_schemas(dsn, [model_schema, scratch])
        for workspace in tmp_path_factory.getbasetemp().parent.glob("dbt_refmerge_*"):
            shutil.rmtree(workspace, ignore_errors=True)


@pytest.fixture
def warehouse_project(tmp_path, pg_dsn, pg_schemas, dbt_executable):
    """A fresh project for tests that edit files; returns (root, config)."""
    root = tmp_path / "project"
    profiles = _build_project(root, tmp_path / "profiles", pg_dsn, dbt_executable, pg_schemas("model"))
    config = AppConfig(
        project_dir=root, profiles_dir=profiles, scratch_schema=pg_schemas("scratch"), dbt_command=(dbt_executable,)
    )
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


def test_check_proves_merge_and_leaves_scratch_schema_empty(proven):
    receipt = _orders_receipt(proven.report)

    assert (receipt.status, receipt.reason_codes) == (VerificationStatus.SNAPSHOT_EQUIVALENT, (ReasonCode.OK,))
    assert receipt.equality == EqualityResult(True, 6, 6, 0, 0)
    assert is_fixable(receipt)
    assert _relations_in(proven.dsn, proven.config.scratch_schema) == []


def test_fix_applies_the_proven_merge(warehouse_project):
    root, config = warehouse_project

    report = RefmergeService().fix(FixRequest(config=config, model_path=Path("models/orders.sql")))

    assert (report.applied, report.reason) == (True, "applied")
    rewritten = (root / "models" / "orders.sql").read_text()
    assert "join orders as order_financials" in rewritten
    assert "order_financials as (" not in rewritten
    assert RefmergeService().scan(ScanRequest(config=config.model_copy(update={"adapter": "postgres"}))).findings == ()


def _with_compiled(node, replace_from: str, replace_to: str):
    compiled = node.compiled_code or ""
    assert replace_from in compiled
    return node.model_copy(update={"compiled_code": compiled.replace(replace_from, replace_to)})


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
def test_divergent_candidate_is_refused(proven, replace_to, status, codes, equality):
    request = proven.request
    tampered = dataclasses.replace(
        request, candidate=_with_compiled(request.candidate, "select * from final", replace_to)
    )

    receipt = verify_postgres(tampered)

    assert (receipt.status, receipt.reason_codes, receipt.equality) == (status, codes, equality)
    assert not is_fixable(receipt)
    assert _relations_in(proven.dsn, proven.config.scratch_schema) == []


def test_unsupported_column_type_is_unverifiable(proven):
    request = proven.request
    as_float = "select order_id, customer_id, amount::double precision from final"
    tampered = dataclasses.replace(
        request,
        baseline=_with_compiled(request.baseline, "select * from final", as_float),
        candidate=_with_compiled(request.candidate, "select * from final", as_float),
    )

    receipt = verify_postgres(tampered)

    assert (receipt.status, receipt.reason_codes) == (
        VerificationStatus.UNVERIFIABLE,
        (ReasonCode.UNSUPPORTED_COMPARISON_TYPE,),
    )
    assert _relations_in(proven.dsn, proven.config.scratch_schema) == []


def test_cleanup_drops_leftover_views_for_the_run_only(proven, pg_schemas):
    import psycopg2

    scratch = pg_schemas("cleanup")
    config = proven.config.model_copy(update={"scratch_schema": scratch})
    run_id = "20260916T120000_0123456789ab"
    ours = "dbt_refmerge_baseline_000_0123456789ab_deadbeef"
    ours_tmp = "dbt_refmerge_candidate_000_0123456789ab_deadbeef__dbt_tmp"
    other_run = "dbt_refmerge_baseline_000_ffffffffffff_deadbeef"
    table_with_our_name = "dbt_refmerge_candidate_000_0123456789ab_cafebabe"
    with psycopg2.connect(proven.dsn) as conn, conn.cursor() as cur:
        cur.execute(f'create schema if not exists "{scratch}"')
        for view in (ours, ours_tmp, other_run):
            cur.execute(f'create view "{scratch}"."{view}" as select 1 as x')
        cur.execute(f'create table "{scratch}"."{table_with_our_name}" (x int)')

    result = RefmergeService().cleanup(CleanupRequest(config=config, run_id=run_id))

    assert result["dropped"] == sorted([ours, ours_tmp]) and result["complete"]
    assert _relations_in(proven.dsn, scratch) == [(other_run, "VIEW"), (table_with_our_name, "BASE TABLE")]


def test_check_compiles_projects_that_use_a_local_package(tmp_path, pg_dsn, pg_schemas, dbt_executable):
    # dbt deps links a local package into dbt_packages; the snapshot must carry it or the baseline compile of
    # stg_orders (which calls the package macro) fails.
    model_schema = pg_schemas("model")
    root = tmp_path / "project"
    package = tmp_path / "shared_macros"
    (package / "macros").mkdir(parents=True)
    (package / "dbt_project.yml").write_text("name: shared_macros\nversion: 1.0.0\nconfig-version: 2\n")
    (package / "macros" / "doubled.sql").write_text("{% macro doubled(col) %}({{ col }} * 2){% endmacro %}\n")
    (root / "models").mkdir(parents=True)
    (root / "dbt_project.yml").write_text("name: it\nversion: 1.0.0\nconfig-version: 2\nprofile: it\n")
    (root / "packages.yml").write_text(f"packages:\n  - local: {package.as_posix()}\n")
    (root / "models" / "stg_orders.sql").write_text(
        "select order_id, customer_id, {{ shared_macros.doubled('amount') }} as amount from ("
        + STG_ORDERS.strip()
        + ") as s\n"
    )
    (root / "models" / "orders.sql").write_text(ORDERS)
    profiles = write_pg_profiles(tmp_path / "profiles", pg_dsn, profile="it", schema=model_schema)
    for args in (["deps"], ["run", "--select", "stg_orders"]):
        subprocess.run(
            [dbt_executable, *args, "--profiles-dir", str(profiles)], cwd=root, check=True, capture_output=True
        )
    assert (root / "dbt_packages" / "shared_macros").is_symlink()
    config = AppConfig(
        project_dir=root, profiles_dir=profiles, scratch_schema=pg_schemas("scratch"), dbt_command=(dbt_executable,)
    )

    receipt = _orders_receipt(RefmergeService().check(CheckRequest(config=config)))

    assert (receipt.status, receipt.reason_codes) == (VerificationStatus.SNAPSHOT_EQUIVALENT, (ReasonCode.OK,))
    assert receipt.equality == EqualityResult(True, 6, 6, 0, 0)


def test_one_check_verifies_several_models_and_keeps_failures_per_model(warehouse_project, pg_dsn):
    # Four merges share one harness run: two prove equivalent, one view cannot be built and one comparison
    # fails at query time (the view is valid SQL, but reading it divides by zero).
    root, config = warehouse_project
    for name in ("orders_copy", "orders_unbuildable", "orders_failing_query"):
        (root / "models" / f"{name}.sql").write_text(ORDERS)
    tampering = {
        "model.it.orders_unbuildable": "select * from final join no_such_relation using (order_id)",
        "model.it.orders_failing_query": "select order_id / (order_id - order_id) as order_id, customer_id, amount from final",
    }

    def tamper(batch):
        assert len(batch) == 4
        return verify_postgres_batch(
            [
                dataclasses.replace(
                    r,
                    baseline=_with_compiled(r.baseline, "select * from final", tampering[r.baseline.unique_id]),
                    candidate=_with_compiled(r.candidate, "select * from final", tampering[r.baseline.unique_id]),
                )
                if r.baseline.unique_id in tampering
                else r
                for r in batch
            ]
        )

    report = RefmergeService(verify_runner=tamper).check(CheckRequest(config=config))

    receipts = {r.model_unique_id: r.receipt for r in report.results}
    for uid in ("model.it.orders", "model.it.orders_copy"):
        assert (receipts[uid].status, receipts[uid].equality) == (
            VerificationStatus.SNAPSHOT_EQUIVALENT,
            EqualityResult(True, 6, 6, 0, 0),
        )
    for uid in tampering:
        assert (receipts[uid].status, receipts[uid].reason_codes) == (
            VerificationStatus.ERROR,
            (ReasonCode.DBT_COMMAND_FAILED,),
        )
    assert all(receipt.cleanup_complete for receipt in receipts.values())
    assert _relations_in(pg_dsn, config.scratch_schema) == []
