"""Source-frontend changes proven through the whole in-memory pipeline.

Compiled SQL is rendered with Jinja itself, so whitespace-control markers strip exactly what dbt
would strip. Each case runs source parse -> match -> group -> qualify -> rewrite -> delta gate.
"""

from pathlib import Path

import pytest
from jinja2 import Environment

from dbt_refmerge.adapters import get_spec
from dbt_refmerge.analyze import FindingStatus, QualifiedDuplicateGroup, group_imports, qualify_group
from dbt_refmerge.artifacts import DependsOnModel, ManifestMetadataModel, ManifestNodeModel, ManifestView
from dbt_refmerge.domain import ReasonCode
from dbt_refmerge.orchestrator import RefmergeService
from dbt_refmerge.rewrite import apply_edits, build_plan
from dbt_refmerge.semantics import match_source_ctes, parse_model
from dbt_refmerge.source import parse_source_model

_JINJA = Environment(autoescape=False)  # noqa: S701 -- renders SQL, never HTML


def _render(raw: str) -> str:
    return _JINJA.from_string(raw).render(ref=lambda name: f'"db"."sch"."{name}"')


def _owner(raw: str) -> tuple[ManifestView, ManifestNodeModel]:
    owner = ManifestNodeModel(
        unique_id="model.p.m",
        resource_type="model",
        package_name="p",
        name="m",
        original_file_path="models/m.sql",
        raw_code=raw,
        compiled_code=_render(raw),
        depends_on=DependsOnModel(nodes=["model.p.stg"]),
    )
    stg = ManifestNodeModel(
        unique_id="model.p.stg",
        resource_type="model",
        package_name="p",
        name="stg",
        original_file_path="models/stg.sql",
    )
    view = ManifestView(
        metadata=ManifestMetadataModel(
            dbt_schema_version="https://schemas.getdbt.com/dbt/manifest/v12.json", dbt_version="1.9.0"
        ),
        nodes={owner.unique_id: owner, stg.unique_id: stg},
        sources={},
        path=Path("manifest.json"),
    )
    return view, owner


def _qualify(raw: str) -> tuple[ManifestNodeModel, QualifiedDuplicateGroup]:
    view, owner = _owner(raw)
    source = parse_source_model(raw.encode())
    matched = match_source_ctes(
        tuple(cte for cte in source.ctes if cte.ref_call is not None),
        parse_model(owner.compiled_code or ""),
        view,
        owner,
    )
    (group,) = group_imports(list(matched), owner.unique_id)
    return owner, qualify_group(group, downstream_refs=source.downstream_refs, nested_names=source.nested_names)


_IMPORTS = (
    "with a as (\n    select\n        id,\n        customer_id\n    from {{ ref('stg') }}\n),\n"
    "b as (\n    select\n        id,\n        amount\n    from {{ ref('stg') }}\n),\n"
)


@pytest.mark.parametrize(
    "tail",
    [
        "wrapper as (with recursive b as (select 1 as id) select id from b)\nselect id from wrapper\n",
        "wrapper as (with x as (select 1 as id), b as (select 2 as id) select id from b)\nselect id from wrapper\n",
        "final as (select 1 as id)\nselect s.id from (with b as (select 3 as id) select id from b) s\n",
    ],
    ids=["nested-recursive", "second-nested-cte", "main-query-nested-with"],
)
def test_nested_cte_shadowing_a_donor_refuses(tail):
    # Each of these used to merge: the delta gate renames every `b` without scoping, so only the
    # source frontend can see that `from b` binds to the nested CTE rather than the donor.
    _owner_node, qualified = _qualify(_IMPORTS + tail)
    assert (qualified.status, qualified.reason_codes) == (
        FindingStatus.NOT_ELIGIBLE,
        (ReasonCode.REFERENCE_BINDING_AMBIGUOUS,),
    )


def test_whitespace_control_refs_merge_byte_exact_and_pass_the_delta_gate():
    # `{{-` / `-}}` strip the whitespace around the tag, which lies inside the import body, so no
    # edit touches it: the canonical ref stays byte-identical and the donor goes as a whole.
    raw = (
        b"with a as (\n"
        b"    select\n"
        b"        id,\n"
        b"        customer_id\n"
        b"    from\n"
        b"    {{- ref('stg') -}}\n"
        b"    where id > 0\n"
        b"),\n"
        b"b as (\n"
        b"    select\n"
        b"        id,\n"
        b"        amount\n"
        b"    from {{+ ref('stg') -}}\n"
        b"    where id > 0\n"
        b"),\n"
        b"final as (select b.id, b.amount from b)\n"
        b"select * from final\n"
    )
    expected = (
        b"with a as (\n"
        b"    select\n"
        b"        id,\n"
        b"        customer_id,\n"
        b"        amount\n"
        b"    from\n"
        b"    {{- ref('stg') -}}\n"
        b"    where id > 0\n"
        b"),\n"
        b"final as (select b.id, b.amount from a as b)\n"
        b"select * from final\n"
    )
    owner, qualified = _qualify(raw.decode())
    assert '"db"."sch"."stg"where id > 0' in (owner.compiled_code or "")
    assert (qualified.status, qualified.reason_codes) == (FindingStatus.MERGE_ELIGIBLE, (ReasonCode.OK,))
    source = parse_source_model(raw)
    plan = build_plan(raw, owner.unique_id, Path("models/m.sql"), (qualified,), source)
    candidate = apply_edits(raw, plan.edits)
    assert candidate == expected
    candidate_node = owner.model_copy(
        update={"raw_code": candidate.decode(), "compiled_code": _render(candidate.decode())}
    )
    RefmergeService()._validate_delta(owner, candidate_node, (qualified,), get_spec("postgres"))
