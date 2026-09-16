"""Compiled-delta gate: the expected transform must match what the rewrite actually produces.

Each case runs the same pipeline as ``check`` in memory: source -> compiled (ref substitution,
like dbt) -> match/group/qualify -> rewrite -> candidate compiled -> ``_validate_delta``.
"""

import re
from pathlib import Path

import pytest

from dbt_refmerge.adapters import get_spec
from dbt_refmerge.analyze import FindingStatus, group_imports, qualify_group
from dbt_refmerge.artifacts import DependsOnModel, ManifestMetadataModel, ManifestNodeModel, ManifestView
from dbt_refmerge.domain import ReasonCode
from dbt_refmerge.errors import SemanticError
from dbt_refmerge.orchestrator import RefmergeService
from dbt_refmerge.rewrite import apply_edits, build_plan
from dbt_refmerge.semantics import match_source_ctes, parse_model
from dbt_refmerge.source import parse_source_model

_REF = re.compile(r"\{\{ ref\('(\w+)'\) \}\}")


def _compile(raw: str) -> str:
    return _REF.sub(lambda m: f'"db"."sch"."{m.group(1)}"', raw)


def _rewrite(raw: str) -> tuple[ManifestNodeModel, ManifestNodeModel, tuple]:
    upstreams = sorted(set(_REF.findall(raw)))
    owner = ManifestNodeModel(
        unique_id="model.p.m",
        resource_type="model",
        package_name="p",
        name="m",
        original_file_path="models/m.sql",
        raw_code=raw,
        compiled_code=_compile(raw),
        depends_on=DependsOnModel(nodes=[f"model.p.{u}" for u in upstreams]),
    )
    nodes = {owner.unique_id: owner}
    for u in upstreams:
        nodes[f"model.p.{u}"] = ManifestNodeModel(
            unique_id=f"model.p.{u}",
            resource_type="model",
            package_name="p",
            name=u,
            original_file_path=f"models/{u}.sql",
        )
    view = ManifestView(
        metadata=ManifestMetadataModel(
            dbt_schema_version="https://schemas.getdbt.com/dbt/manifest/v12.json", dbt_version="1.9.0"
        ),
        nodes=nodes,
        sources={},
        path=Path("manifest.json"),
    )
    source = parse_source_model(raw.encode())
    matched = match_source_ctes(
        tuple(c for c in source.ctes if c.ref_call is not None),
        parse_model(owner.compiled_code or ""),
        view,
        owner,
    )
    qualified = tuple(
        qualify_group(g, downstream_refs=source.downstream_refs, nested_names=source.nested_names)
        for g in group_imports(list(matched), owner.unique_id)
    )
    assert qualified and all(q.status is FindingStatus.MERGE_ELIGIBLE for q in qualified)
    plan = build_plan(raw.encode(), owner.unique_id, Path("models/m.sql"), qualified, source)
    candidate = apply_edits(raw.encode(), plan.edits).decode()
    candidate_node = owner.model_copy(update={"raw_code": candidate, "compiled_code": _compile(candidate)})
    return owner, candidate_node, qualified


def _gate(raw: str) -> str:
    owner, candidate_node, qualified = _rewrite(raw)
    RefmergeService()._validate_delta(owner, candidate_node, qualified, get_spec("postgres"))
    return candidate_node.raw_code


def _model(final: str, *, a: str = "id, customer_id", b: str = "id, amount", a_name="a", b_name="b") -> str:
    def select_list(projections: str) -> str:
        return ",\n".join(f"        {p.strip()}" for p in projections.split(","))

    return (
        f"with {a_name} as (\n    select\n{select_list(a)}\n    from {{{{ ref('stg') }}}}\n),\n"
        f"{b_name} as (\n    select\n{select_list(b)}\n    from {{{{ ref('stg') }}}}\n),\n"
        f"{final}\n"
    )


def test_delta_gate_accepts_aliased_donor_reference():
    candidate = _gate(
        _model("final as (select a.customer_id, x.amount from a join b as x using (id))\nselect * from final")
    )
    assert "join a as x using (id)" in candidate


def test_delta_gate_accepts_bare_donor_reference():
    # B3: README / golden shape. The rewrite adds `as b`; the gate must not call that drift.
    candidate = _gate(_model("final as (select a.customer_id, b.amount from a join b using (id))\nselect * from final"))
    assert "join a as b using (id)" in candidate


def test_delta_gate_accepts_terminal_donor_reference():
    candidate = _gate(_model("final as (select b.amount from b)\nselect * from final"))
    assert "from a as b" in candidate


@pytest.mark.parametrize(
    ("b_projection", "final"),
    [
        ("id, amount as amt", "final as (select x.amt from a join b as x using (id))\nselect * from final"),
        ('id, "Amount"', 'final as (select x."Amount" from a join b as x using (id))\nselect * from final'),
    ],
    ids=["renamed", "quoted"],
)
def test_delta_gate_accepts_added_projection_spelling(b_projection, final):
    # B5: added projections must be rebuilt from their SQL, not from the folded output name.
    _gate(_model(final, b=b_projection))


def test_delta_gate_accepts_two_independent_groups():
    # B4: the expected tree must apply every group before comparing.
    raw = (
        "with a as (select id from {{ ref('stg') }}),\n"
        "b as (select id from {{ ref('stg') }}),\n"
        "c as (select customer_id from {{ ref('customers') }}),\n"
        "d as (select customer_id from {{ ref('customers') }}),\n"
        "final as (select x.id, y.customer_id from b as x join d as y on true)\n"
        "select * from final\n"
    )
    candidate = _gate(raw)
    assert "from a as x join c as y on true" in candidate


def test_delta_gate_leaves_physical_table_named_like_donor_alone():
    # B6: only unqualified references to the donor CTE are redirected, not "db"."sch"."stg".
    raw = _model(
        "final as (select a.customer_id, x.amount from a join stg as x using (id))\nselect * from final",
        b_name="stg",
    )
    candidate = _gate(raw)
    assert "join a as x using (id)" in candidate


def test_delta_gate_refuses_drifted_candidate():
    owner, candidate_node, qualified = _rewrite(
        _model("final as (select a.customer_id, x.amount from a join b as x using (id))\nselect * from final")
    )
    drifted = candidate_node.model_copy(update={"compiled_code": (candidate_node.compiled_code or "") + " limit 1"})
    with pytest.raises(SemanticError) as exc_info:
        RefmergeService()._validate_delta(owner, drifted, qualified, get_spec("postgres"))
    assert exc_info.value.reason_code is ReasonCode.COMPILE_DRIFT


def test_delta_gate_accepts_implicit_donor_alias():
    candidate = _gate(
        _model("final as (select a.customer_id, x.amount from a join b x using (id))\nselect * from final")
    )
    assert "join a x using (id)" in candidate


def test_delta_gate_refuses_unchanged_candidate():
    # A candidate that still compiles to the baseline did not apply the merge the plan promised.
    owner, _candidate_node, qualified = _rewrite(
        _model("final as (select a.customer_id, x.amount from a join b as x using (id))\nselect * from final")
    )
    with pytest.raises(SemanticError) as exc_info:
        RefmergeService()._validate_delta(owner, owner, qualified, get_spec("postgres"))
    assert exc_info.value.reason_code is ReasonCode.COMPILE_DRIFT
