"""Golden file regression: committed expected.sql must equal the planner output."""

from pathlib import Path

from dbt_refmerge.analyze import group_imports, qualify_group
from dbt_refmerge.artifacts import (
    DependsOnModel,
    ManifestMetadataModel,
    ManifestNodeModel,
    ManifestView,
)
from dbt_refmerge.rewrite import apply_edits, build_plan
from dbt_refmerge.semantics import match_source_ctes, parse_model
from dbt_refmerge.source import parse_source_model

CASES = ("disjoint_projections",)

COMPILED = {
    "disjoint_projections": (
        "with orders as (select order_id, customer_id from db.sch.stg_orders), "
        "order_financials as (select order_id, amount from db.sch.stg_orders), "
        "final as (select orders.order_id, order_financials.amount from orders "
        "join order_financials on orders.order_id = order_financials.order_id) "
        "select * from final"
    ),
}


def _view(upstream: str) -> tuple[ManifestView, ManifestNodeModel]:
    owner = ManifestNodeModel(
        unique_id="model.p.m",
        resource_type="model",
        package_name="p",
        name="m",
        original_file_path="m.sql",
        depends_on=DependsOnModel(macros=[], nodes=[f"model.p.{upstream}"]),
        config={"materialized": "view"},
    )
    up = ManifestNodeModel(
        unique_id=f"model.p.{upstream}",
        resource_type="model",
        package_name="p",
        name=upstream,
        original_file_path="stg.sql",
        depends_on=DependsOnModel(macros=[], nodes=[]),
        config={"materialized": "view"},
    )
    meta = ManifestMetadataModel(
        dbt_schema_version="https://schemas.getdbt.com/dbt/manifest/v12.json",
        dbt_version="1.8.0",
    )
    return (
        ManifestView(
            metadata=meta,
            nodes={owner.unique_id: owner, up.unique_id: up},
            sources={},
            path=Path("m.json"),
        ),
        owner,
    )


def test_golden_cases_match_planner():
    root = Path(__file__).parent
    for case in CASES:
        raw = (root / case / "input.sql").read_bytes()
        expected = (root / case / "expected.sql").read_bytes()
        view, owner = _view("stg_orders")
        src = parse_source_model(raw)
        matched = match_source_ctes(
            tuple(c for c in src.ctes if c.ref_call is not None),
            parse_model(COMPILED[case], "postgres"),
            view,
            owner,
        )
        groups = group_imports(list(matched), owner.unique_id)
        assert len(groups) == 1
        qualified = qualify_group(groups[0], whole_model_ok=True)
        plan = build_plan(raw, owner.unique_id, Path("m.sql"), (qualified,), src)
        assert apply_edits(raw, plan.edits) == expected
