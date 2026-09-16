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


def _run_case(raw_str: str, compiled_str: str, view_owner=None):
    from dbt_refmerge.errors import ReasonCode  # noqa: F401 -- re-export guard

    raw = raw_str.encode()
    view, owner = view_owner or _manifest_for_refs()
    src = parse_source_model(raw)
    matched = match_source_ctes(
        tuple(c for c in src.ctes if c.ref_call is not None),
        parse_model(compiled_str, "postgres"),
        view,
        owner,
    )
    groups = group_imports(list(matched), owner.unique_id)
    assert len(groups) == 1
    return raw, src, owner, groups[0]


def _norm(text: str) -> str:
    import re

    return re.sub(r"\s+", " ", text)


def test_three_members_collapse_to_first():
    raw_str = (
        "with a as (\n    select\n        order_id,\n        customer_id\n    from {{ ref('stg') }}\n),\n"
        "b as (\n    select\n        order_id,\n        amount\n    from {{ ref('stg') }}\n),\n"
        "c as (\n    select\n        order_id,\n        region\n    from {{ ref('stg') }}\n),\n"
        "final as (\n    select a.order_id from a join b on a.order_id = b.order_id "
        "join c on a.order_id = c.order_id\n)\n"
        "select * from final\n"
    )
    compiled = (
        "with a as (select order_id, customer_id from db.sch.stg), "
        "b as (select order_id, amount from db.sch.stg), "
        "c as (select order_id, region from db.sch.stg), "
        "final as (select a.order_id from a join b on a.order_id = b.order_id "
        "join c on a.order_id = c.order_id) "
        "select * from final"
    )
    raw, src, owner, group = _run_case(raw_str, compiled)
    assert len(group.imports) == 3
    q = qualify_group(group, whole_model_ok=True)
    assert q.status.value == "merge_eligible"
    plan = build_plan(raw, owner.unique_id, Path("m.sql"), (q,), src)
    text = apply_edits(raw, plan.edits).decode()
    assert "b as (" not in text and "c as (" not in text
    for col in ("customer_id", "amount", "region"):
        assert col in text
    assert "a as b" in text and "a as c" in text


def test_projection_union_overlapping_columns():
    raw_str = (
        "with base as (\n    select\n        order_id,\n        customer_id\n    from {{ ref('stg') }}\n),\n"
        "extra as (\n    select\n        customer_id,\n        amount,\n        region\n    from {{ ref('stg') }}\n),\n"
        "final as (\n    select base.order_id from base "
        "join extra on base.customer_id = extra.customer_id\n)\n"
        "select * from final\n"
    )
    compiled = (
        "with base as (select order_id, customer_id from db.sch.stg), "
        "extra as (select customer_id, amount, region from db.sch.stg), "
        "final as (select base.order_id from base join extra on base.customer_id = extra.customer_id) "
        "select * from final"
    )
    raw, src, owner, group = _run_case(raw_str, compiled)
    q = qualify_group(group, whole_model_ok=True)
    assert q.status.value == "merge_eligible"
    plan = build_plan(raw, owner.unique_id, Path("m.sql"), (q,), src)
    normed = _norm(apply_edits(raw, plan.edits).decode())
    # canonical order kept, missing outputs appended once each in first-seen order
    assert "order_id, customer_id, amount, region" in normed
    assert normed.split("final as")[0].count("customer_id") == 1


def test_projection_collision_refused():
    from dbt_refmerge.errors import ReasonCode

    raw_str = (
        "with a as (\n    select\n        amount as x\n    from {{ ref('stg') }}\n),\n"
        "b as (\n    select\n        region as x\n    from {{ ref('stg') }}\n),\n"
        "final as (\n    select a.x from a join b on a.x = b.x\n)\n"
        "select * from final\n"
    )
    compiled = (
        "with a as (select amount as x from db.sch.stg), "
        "b as (select region as x from db.sch.stg), "
        "final as (select a.x from a join b on a.x = b.x) "
        "select * from final"
    )
    _, _, _, group = _run_case(raw_str, compiled)
    q = qualify_group(group, whole_model_ok=True)
    assert q.status.value == "not_eligible"
    assert ReasonCode.PROJECTION_COLLISION in q.reason_codes


def test_different_predicate_refused():
    from dbt_refmerge.errors import ReasonCode

    raw_str = (
        "with a as (\n    select\n        order_id\n    from {{ ref('stg') }}\n    where amount > 100\n),\n"
        "b as (\n    select\n        order_id\n    from {{ ref('stg') }}\n    where amount > 500\n),\n"
        "final as (\n    select a.order_id from a join b on a.order_id = b.order_id\n)\n"
        "select * from final\n"
    )
    compiled = (
        "with a as (select order_id from db.sch.stg where amount > 100), "
        "b as (select order_id from db.sch.stg where amount > 500), "
        "final as (select a.order_id from a join b on a.order_id = b.order_id) "
        "select * from final"
    )
    _, _, _, group = _run_case(raw_str, compiled)
    q = qualify_group(group, whole_model_ok=True)
    assert q.status.value == "not_eligible"
    assert ReasonCode.DIFFERENT_PREDICATE in q.reason_codes


def test_source_kind_matches_and_merges():
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
        depends_on=DependsOnModel(macros=[], nodes=["source.p.raw.orders"]),
        config={"materialized": "view"},
    )
    meta = ManifestMetadataModel(
        dbt_schema_version="https://schemas.getdbt.com/dbt/manifest/v12.json", dbt_version="1.8.0"
    )
    view = ManifestView(
        metadata=meta,
        nodes={owner.unique_id: owner},
        sources={"source.p.raw.orders": {"source_name": "raw", "name": "orders"}},
        path=Path("m.json"),
    )
    raw_str = (
        "with a as (\n    select\n        order_id,\n        customer_id\n    from {{ source('raw', 'orders') }}\n),\n"
        "b as (\n    select\n        order_id,\n        amount\n    from {{ source('raw', 'orders') }}\n),\n"
        "final as (\n    select a.order_id from a join b on a.order_id = b.order_id\n)\n"
        "select * from final\n"
    )
    compiled = (
        "with a as (select order_id, customer_id from db.sch.orders), "
        "b as (select order_id, amount from db.sch.orders), "
        "final as (select a.order_id from a join b on a.order_id = b.order_id) "
        "select * from final"
    )
    raw, src, owner2, group = _run_case(raw_str, compiled, (view, owner))
    assert group.upstream_unique_id == "source.p.raw.orders"
    q = qualify_group(group, whole_model_ok=True)
    assert q.status.value == "merge_eligible"
    plan = build_plan(raw, owner2.unique_id, Path("m.sql"), (q,), src)
    text = apply_edits(raw, plan.edits).decode()
    assert "b as (" not in text and "amount" in text
