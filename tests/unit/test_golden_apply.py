"""Golden end-to-end source rewrite + safe apply + CLI smoke."""

import hashlib
from pathlib import Path

import pytest

from dbt_refmerge.analyze import group_imports, qualify_group
from dbt_refmerge.errors import SourceChangedError
from dbt_refmerge.orchestrator import apply_verified_source
from dbt_refmerge.rewrite import apply_edits, build_plan
from dbt_refmerge.semantics import match_source_ctes, parse_model
from dbt_refmerge.source import parse_source_model


def _manifest():
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


def test_golden_trace_from_guide():
    raw = (
        b"with orders as (\n\n    select\n        order_id,\n        customer_id\n\n    from {{ ref('stg') }}\n\n),\n\n"
        b"order_financials as (\n\n    select\n        order_id,\n        amount,\n        tax\n\n    from {{ ref('stg') }}\n\n),\n\n"
        b"final as (\n\n    select\n        orders.order_id,\n        orders.customer_id,\n        order_financials.amount,\n        order_financials.tax\n\n"
        b"    from orders\n    left join order_financials\n        on orders.order_id = order_financials.order_id\n\n)\n\n"
        b"select * from final\n"
    )
    compiled = (
        "with orders as (select order_id, customer_id from db.sch.stg), "
        "order_financials as (select order_id, amount, tax from db.sch.stg), "
        "final as (select orders.order_id, orders.customer_id, order_financials.amount, order_financials.tax "
        "from orders left join order_financials on orders.order_id = order_financials.order_id) "
        "select * from final"
    )
    view, owner = _manifest()
    src = parse_source_model(raw)
    matched = match_source_ctes(
        tuple(c for c in src.ctes if c.ref_call is not None),
        parse_model(compiled, "postgres"),
        view,
        owner,
    )
    groups = group_imports(list(matched), owner.unique_id)
    assert len(groups) == 1
    q = qualify_group(groups[0], whole_model_ok=True)
    plan = build_plan(raw, owner.unique_id, Path("m.sql"), (q,), src)
    cand = apply_edits(raw, plan.edits)
    text = cand.decode()
    assert "left join orders as order_financials" in text
    assert "amount" in text and "tax" in text
    assert hashlib.sha256(cand).hexdigest() == plan.candidate_source_sha256


def test_apply_roundtrip_and_race_guard(tmp_path):
    target = tmp_path / "m.sql"
    target.write_bytes(b"select 1\n")
    orig = hashlib.sha256(b"select 1\n").hexdigest()
    cand = b"select 1, 2\n"
    cand_sha = hashlib.sha256(cand).hexdigest()
    apply_verified_source(
        target,
        expected_original_sha256=orig,
        candidate_bytes=cand,
        expected_candidate_sha256=cand_sha,
    )
    assert target.read_bytes() == cand
    # stale receipt must not apply
    with pytest.raises(SourceChangedError):
        apply_verified_source(
            target,
            expected_original_sha256=orig,
            candidate_bytes=cand,
            expected_candidate_sha256=cand_sha,
        )


def test_cli_help_runs():
    from typer.testing import CliRunner

    from dbt_refmerge.cli import app

    runner = CliRunner()
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "scan" in result.output and "check" in result.output


def test_cli_version_matches_package():
    from typer.testing import CliRunner

    from dbt_refmerge import __version__
    from dbt_refmerge.cli import app

    result = CliRunner().invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.output == f"dbt-refmerge {__version__}\n"
