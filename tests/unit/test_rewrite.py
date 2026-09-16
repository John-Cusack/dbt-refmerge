"""Rewrite planner golden-path tests."""

from pathlib import Path

from dbt_refmerge.analyze import group_imports, qualify_group
from dbt_refmerge.rewrite import apply_edits, build_plan, validate_edits
from dbt_refmerge.semantics import match_source_ctes, parse_model
from dbt_refmerge.source import parse_source_model


def _manifest_for_refs():
    from dbt_refmerge.artifacts import (
        DependsOnModel,
        ManifestMetadataModel,
        ManifestNodeModel,
        ManifestView,
    )

    owner = ManifestNodeModel(
        unique_id="model.p.m",
        resource_type="model",
        package_name="p",
        name="m",
        original_file_path="m.sql",
        depends_on=DependsOnModel(macros=[], nodes=["model.p.stg"]),
        config={"materialized": "view"},
    )
    up = ManifestNodeModel(
        unique_id="model.p.stg",
        resource_type="model",
        package_name="p",
        name="stg",
        original_file_path="stg.sql",
        depends_on=DependsOnModel(macros=[], nodes=[]),
        config={"materialized": "view"},
    )
    meta = ManifestMetadataModel(
        dbt_schema_version="https://schemas.getdbt.com/dbt/manifest/v12.json", dbt_version="1.8.0"
    )
    return ManifestView(
        metadata=meta,
        nodes={owner.unique_id: owner, up.unique_id: up},
        sources={},
        path=Path("m.json"),
    ), owner


BASE = (
    "with orders as (\n    select\n        order_id,\n        customer_id\n    from {{ ref('stg') }}\n),\n"
    "order_financials as (\n    select\n        order_id,\n        amount\n    from {{ ref('stg') }}\n),\n"
    "final as (\n    select orders.order_id from orders join order_financials on orders.order_id = order_financials.order_id\n)\n"
    "select * from final\n"
)

COMPILED = (
    "with orders as (select order_id, customer_id from db.sch.stg), "
    "order_financials as (select order_id, amount from db.sch.stg), "
    "final as (select orders.order_id from orders join order_financials on orders.order_id = order_financials.order_id) "
    "select * from final"
)


def _qualified():
    raw = BASE.encode()
    view, owner = _manifest_for_refs()
    src = parse_source_model(raw)
    matched = match_source_ctes(
        tuple(c for c in src.ctes if c.ref_call is not None),
        parse_model(COMPILED, "postgres"),
        view,
        owner,
    )
    groups = group_imports(list(matched), owner.unique_id)
    assert len(groups) == 1
    q = qualify_group(groups[0], whole_model_ok=True)
    assert q.status.value == "merge_eligible"
    return raw, src, owner, (q,)


def test_plan_merges_and_redirects():
    raw, src, owner, groups = _qualified()
    plan = build_plan(raw, owner.unique_id, Path("m.sql"), groups, src)
    cand = apply_edits(raw, plan.edits)
    text = cand.decode()
    assert "order_financials as (" not in text
    assert "amount" in text  # appended to canonical
    assert "orders as order_financials" in text
    # no bytes outside spans changed
    validate_edits(plan.edits, len(raw))


def test_plan_deterministic_hash():
    raw, src, owner, groups = _qualified()
    p1 = build_plan(raw, owner.unique_id, Path("m.sql"), groups, src)
    p2 = build_plan(raw, owner.unique_id, Path("m.sql"), groups, src)
    assert p1.candidate_source_sha256 == p2.candidate_source_sha256
