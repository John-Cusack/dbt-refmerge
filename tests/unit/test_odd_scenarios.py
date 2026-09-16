"""Odd-scenario regressions for the fail-closed rewrite safety kernel."""

from pathlib import Path

import pytest

from dbt_refmerge.adapters import spec_for_dialect
from dbt_refmerge.analyze import FindingStatus, group_imports, qualify_group
from dbt_refmerge.artifacts import DependsOnModel, ManifestMetadataModel, ManifestNodeModel, ManifestView
from dbt_refmerge.domain import ReasonCode
from dbt_refmerge.errors import RewriteError, SourceParseError
from dbt_refmerge.orchestrator import detect_source_duplicates
from dbt_refmerge.rewrite import apply_edits, build_plan
from dbt_refmerge.semantics import analyze_volatility, match_source_ctes, parse_model
from dbt_refmerge.source import decode_source, parse_source_model


def _manifest_for_refs() -> tuple[ManifestView, ManifestNodeModel]:
    owner = ManifestNodeModel(
        unique_id="model.p.m",
        resource_type="model",
        package_name="p",
        name="m",
        original_file_path="m.sql",
        depends_on=DependsOnModel(macros=[], nodes=["model.p.stg"]),
        config={"materialized": "view"},
    )
    upstream = ManifestNodeModel(
        unique_id="model.p.stg",
        resource_type="model",
        package_name="p",
        name="stg",
        original_file_path="stg.sql",
        depends_on=DependsOnModel(macros=[], nodes=[]),
        config={"materialized": "view"},
    )
    metadata = ManifestMetadataModel(
        dbt_schema_version="https://schemas.getdbt.com/dbt/manifest/v12.json",
        dbt_version="1.8.0",
    )
    return (
        ManifestView(
            metadata=metadata,
            nodes={owner.unique_id: owner, upstream.unique_id: upstream},
            sources={},
            path=Path("m.json"),
        ),
        owner,
    )


def _run_case(
    raw: str | bytes,
    compiled: str,
    view_owner=None,
    *,
    dialect: str = "postgres",
    fold_unquoted=None,
):
    raw_bytes = raw.encode() if isinstance(raw, str) else raw
    view, owner = view_owner or _manifest_for_refs()
    fold = fold_unquoted or spec_for_dialect(dialect).fold_unquoted
    source = parse_source_model(raw_bytes, fold_unquoted=fold)
    matched = match_source_ctes(
        tuple(cte for cte in source.ctes if cte.ref_call is not None),
        parse_model(compiled, dialect),
        view,
        owner,
    )
    groups = group_imports(list(matched), owner.unique_id)
    assert len(groups) == 1
    return raw_bytes, source, owner, groups[0]


def _source(
    final: str,
    *,
    a_projection: str = "id,\n        customer_id",
    b_projection: str = "id,\n        amount",
    a_where: str = "",
    b_where: str = "",
    a_name: str = "a",
    b_name: str = "b",
    ref_name: str = "stg",
    trailing_newline: bool = True,
) -> str:
    separator = "" if final.lstrip().lower().startswith("select") else ","
    text = (
        f"with {a_name} as (\n"
        f"    select\n"
        f"        {a_projection}\n"
        f"    from {{{{ ref('{ref_name}') }}}}{a_where}\n"
        f"),\n"
        f"{b_name} as (\n"
        f"    select\n"
        f"        {b_projection}\n"
        f"    from {{{{ ref('{ref_name}') }}}}{b_where}\n"
        f"){separator}\n"
        f"{final}"
    )
    if trailing_newline and not text.endswith("\n"):
        text += "\n"
    return text


def _compiled(
    final: str,
    *,
    a_projection: str = "id, customer_id",
    b_projection: str = "id, amount",
    a_where: str = "",
    b_where: str = "",
    a_name: str = "a",
    b_name: str = "b",
    relation: str = "db.sch.stg",
) -> str:
    separator = "" if final.lstrip().lower().startswith("select") else ","
    return (
        f"with {a_name} as (select {a_projection} from {relation}{a_where}), "
        f"{b_name} as (select {b_projection} from {relation}{b_where}){separator} "
        f"{final}"
    )


def _qualified(
    raw: str | bytes,
    compiled: str,
    *,
    dialect: str = "postgres",
    fold_unquoted=None,
    view_owner=None,
    whole_model_ok: bool = True,
):
    raw_bytes, source, owner, group = _run_case(
        raw,
        compiled,
        view_owner,
        dialect=dialect,
        fold_unquoted=fold_unquoted,
    )
    qualified = qualify_group(
        group,
        downstream_refs=source.downstream_refs,
        whole_model_ok=whole_model_ok,
        nested_names=source.nested_names,
    )
    return raw_bytes, source, owner, group, qualified


def _candidate(raw: str | bytes, compiled: str, **kwargs) -> bytes:
    raw_bytes, source, owner, _group, qualified = _qualified(raw, compiled, **kwargs)
    assert qualified.status is FindingStatus.MERGE_ELIGIBLE
    plan = build_plan(raw_bytes, owner.unique_id, Path("m.sql"), (qualified,), source)
    return apply_edits(raw_bytes, plan.edits)


def _manifest_for_two_refs() -> tuple[ManifestView, ManifestNodeModel]:
    owner = ManifestNodeModel(
        unique_id="model.p.m",
        resource_type="model",
        package_name="p",
        name="m",
        original_file_path="m.sql",
        depends_on=DependsOnModel(macros=[], nodes=["model.p.stg", "model.p.customers"]),
        config={"materialized": "view"},
    )
    upstreams = {
        name: ManifestNodeModel(
            unique_id=f"model.p.{name}",
            resource_type="model",
            package_name="p",
            name=name,
            original_file_path=f"{name}.sql",
            depends_on=DependsOnModel(macros=[], nodes=[]),
            config={"materialized": "view"},
        )
        for name in ("stg", "customers")
    }
    metadata = ManifestMetadataModel(
        dbt_schema_version="https://schemas.getdbt.com/dbt/manifest/v12.json",
        dbt_version="1.8.0",
    )
    return (
        ManifestView(
            metadata=metadata,
            nodes={owner.unique_id: owner, **{node.unique_id: node for node in upstreams.values()}},
            sources={},
            path=Path("m.json"),
        ),
        owner,
    )


def test_added_projection_cannot_capture_unqualified_downstream_column():
    final = (
        "final as (\n"
        "    select customer_id\n"
        "    from b\n"
        "    join (select id, customer_id from dim_customers) d using (id)\n"
        ")\n"
        "select customer_id from final"
    )
    compiled_final = (
        "final as (select customer_id from b "
        "join (select id, customer_id from dim_customers) d using (id)) "
        "select customer_id from final"
    )
    *_unused, qualified = _qualified(_source(final), _compiled(compiled_final))
    assert qualified.status is FindingStatus.NOT_ELIGIBLE
    assert qualified.reason_codes == (ReasonCode.REFERENCE_BINDING_AMBIGUOUS,)


def test_downstream_qualified_star_refuses_projection_union():
    raw = _source("select b.* from b")
    compiled = _compiled("select b.* from b")
    *_unused, qualified = _qualified(raw, compiled)
    assert qualified.status is FindingStatus.NOT_ELIGIBLE
    assert qualified.reason_codes == (ReasonCode.UNSUPPORTED_IMPORT_SHAPE,)


def test_comma_join_donor_binding_refuses():
    final = "final as (select a.id from a, b where a.id = b.id) select a.id from final"
    *_unused, qualified = _qualified(_source(final), _compiled(final))
    assert qualified.status is FindingStatus.NOT_ELIGIBLE
    assert qualified.reason_codes == (ReasonCode.REFERENCE_BINDING_AMBIGUOUS,)


def test_cross_join_keyword_is_not_an_implicit_alias():
    final = "final as (select b.id from b cross join dim) select b.id from final"
    candidate = _candidate(_source(final), _compiled(final)).decode()
    assert "from a as b cross join dim" in candidate
    assert "b as (" not in candidate


def test_natural_join_refuses_projection_union():
    final = "final as (select a.id from a natural join b) select a.id from final"
    *_unused, qualified = _qualified(_source(final), _compiled(final))
    assert qualified.status is FindingStatus.NOT_ELIGIBLE
    assert qualified.reason_codes == (ReasonCode.REFERENCE_BINDING_AMBIGUOUS,)


def test_canonical_terminal_comment_refuses_projection_append():
    raw = _source(
        "final as (select b.id from b) select b.id from final",
        a_projection="id,\n        customer_id -- describes customer_id",
    )
    compiled = _compiled("final as (select b.id from b) select b.id from final")
    raw_bytes, source, owner, _group, qualified = _qualified(raw, compiled)
    assert qualified.status is FindingStatus.MERGE_ELIGIBLE
    with pytest.raises(RewriteError) as exc_info:
        build_plan(raw_bytes, owner.unique_id, Path("m.sql"), (qualified,), source)
    assert exc_info.value.reason_code is ReasonCode.COMMENT_RELOCATION_UNSUPPORTED


@pytest.mark.parametrize(
    "donor_suffix",
    [
        "    select\n        id,\n        amount -- donor explanation\n    from {{ ref('stg') }}\n)",
        "    select\n        id,\n        amount\n    from {{ ref('stg') }}\n) /* donor ownership note */",
    ],
)
def test_donor_comments_outside_select_span_refuse(donor_suffix):
    raw = (
        "with a as (\n"
        "    select\n"
        "        id,\n"
        "        customer_id\n"
        "    from {{ ref('stg') }}\n"
        "),\n"
        f"b as (\n{donor_suffix},\n"
        "final as (select b.id from b)\n"
        "select b.id from final\n"
    )
    compiled = _compiled("final as (select b.id from b) select b.id from final")
    raw_bytes, source, owner, _group, qualified = _qualified(raw, compiled)
    with pytest.raises(RewriteError) as exc_info:
        build_plan(raw_bytes, owner.unique_id, Path("m.sql"), (qualified,), source)
    assert exc_info.value.reason_code is ReasonCode.COMMENT_RELOCATION_UNSUPPORTED


def test_jinja_block_straddling_donor_deletion_refuses():
    raw = (
        "with a as (\n"
        "    select\n"
        "        id,\n"
        "        customer_id\n"
        "    from {{ ref('stg') }}\n"
        "),\n"
        "{% if var('include_b') %}\n"
        "b as (\n"
        "    select\n"
        "        id,\n"
        "        amount\n"
        "    from {{ ref('stg') }}\n"
        ")\n"
        "{% endif %},\n"
        "final as (select b.id from b)\n"
        "select b.id from final\n"
    )
    compiled = _compiled("final as (select b.id from b) select b.id from final")
    raw_bytes, source, owner, _group, qualified = _qualified(raw, compiled)
    with pytest.raises(RewriteError) as exc_info:
        build_plan(raw_bytes, owner.unique_id, Path("m.sql"), (qualified,), source)
    assert exc_info.value.reason_code is ReasonCode.COMMENT_RELOCATION_UNSUPPORTED


def test_jinja_comma_overlapping_last_donor_deletion_refuses():
    raw = (
        "with a as (\n"
        "    select\n"
        "        id,\n"
        "        customer_id\n"
        "    from {{ ref('stg') }}\n"
        "),\n"
        "{{ config(tags=['safety', 'imports']) }}\n"
        "b as (\n"
        "    select\n"
        "        id,\n"
        "        amount\n"
        "    from {{ ref('stg') }}\n"
        ")\n"
        "select b.id from b\n"
    )
    compiled = (
        "with a as (select id, customer_id from db.sch.stg), "
        "b as (select id, amount from db.sch.stg) select b.id from b"
    )
    raw_bytes, source, owner, _group, qualified = _qualified(raw, compiled)
    assert qualified.status is FindingStatus.MERGE_ELIGIBLE
    with pytest.raises(RewriteError) as exc_info:
        build_plan(raw_bytes, owner.unique_id, Path("m.sql"), (qualified,), source)
    assert exc_info.value.reason_code is ReasonCode.COMMENT_RELOCATION_UNSUPPORTED


def test_sql_comment_comma_overlapping_last_donor_deletion_refuses():
    raw = (
        "with a as (\n"
        "    select\n"
        "        id,\n"
        "        customer_id\n"
        "    from {{ ref('stg') }}\n"
        "),\n"
        "/* separator, ownership */\n"
        "b as (\n"
        "    select\n"
        "        id,\n"
        "        amount\n"
        "    from {{ ref('stg') }}\n"
        ")\n"
        "select b.id from b\n"
    )
    compiled = (
        "with a as (select id, customer_id from db.sch.stg), "
        "b as (select id, amount from db.sch.stg) select b.id from b"
    )
    raw_bytes, source, owner, _group, qualified = _qualified(raw, compiled)
    assert qualified.status is FindingStatus.MERGE_ELIGIBLE
    with pytest.raises(RewriteError) as exc_info:
        build_plan(raw_bytes, owner.unique_id, Path("m.sql"), (qualified,), source)
    assert exc_info.value.reason_code is ReasonCode.COMMENT_RELOCATION_UNSUPPORTED


@pytest.mark.parametrize(
    "terminal_tail",
    [
        "\n-- donor tail, ownership\n",
        "\n{{ config(tags=['terminal']) }}\n",
    ],
)
def test_terminal_donor_tail_comment_or_jinja_refuses(terminal_tail):
    raw = (
        "with a as (\n"
        "    select\n"
        "        id,\n"
        "        customer_id\n"
        "    from {{ ref('stg') }}\n"
        "),\n"
        "b as (\n"
        "    select\n"
        "        id,\n"
        "        amount\n"
        "    from {{ ref('stg') }}\n"
        ")"
        f"{terminal_tail}"
        "select b.id from b\n"
    )
    compiled = (
        "with a as (select id, customer_id from db.sch.stg), "
        "b as (select id, amount from db.sch.stg) select b.id from b"
    )
    raw_bytes, source, owner, _group, qualified = _qualified(raw, compiled)
    assert qualified.status is FindingStatus.MERGE_ELIGIBLE
    with pytest.raises(RewriteError) as exc_info:
        build_plan(raw_bytes, owner.unique_id, Path("m.sql"), (qualified,), source)
    assert exc_info.value.reason_code is ReasonCode.COMMENT_RELOCATION_UNSUPPORTED


def test_none_fold_predicate_case_remains_semantic():
    final = "select a.id from a join b on a.id = b.id"
    raw = _source(final, a_projection="id", b_projection="id", a_where=" where Status = 1", b_where=" where status = 1")
    compiled = _compiled(
        final, a_projection="id", b_projection="id", a_where=" where Status = 1", b_where=" where status = 1"
    )
    *_unused, qualified = _qualified(raw, compiled, dialect="clickhouse")
    assert qualified.status is FindingStatus.NOT_ELIGIBLE
    assert qualified.reason_codes == (ReasonCode.DIFFERENT_PREDICATE,)


def test_quoted_cte_case_maps_without_folding():
    final = 'final as (select "B".amount from "B") select "B".amount from final'
    candidate = _candidate(
        _source(final, a_name='"A"', b_name='"B"'),
        _compiled(final, a_name='"A"', b_name='"B"'),
    ).decode()
    assert '"B" as (' not in candidate
    assert 'from "A" as "B"' in candidate


def test_none_fold_reparse_preserves_case_distinct_ctes():
    raw = (
        "with a as (\n"
        "    select\n"
        "        id,\n"
        "        customer_id\n"
        "    from {{ ref('stg') }}\n"
        "),\n"
        "b as (\n"
        "    select\n"
        "        id,\n"
        "        amount\n"
        "    from {{ ref('stg') }}\n"
        "),\n"
        "Foo as (select 1 as x),\n"
        "foo as (select 2 as x),\n"
        "final as (select b.id from b)\n"
        "select b.id from final\n"
    )
    compiled = (
        "with a as (select id, customer_id from db.sch.stg), "
        "b as (select id, amount from db.sch.stg), "
        "Foo as (select 1 as x), foo as (select 2 as x), "
        "final as (select b.id from b) select b.id from final"
    )
    candidate = _candidate(raw, compiled, dialect="clickhouse").decode()
    assert "Foo as (" in candidate
    assert "foo as (" in candidate
    assert "b as (" not in candidate


def test_bigquery_trailing_comma_style_merges():
    raw = _source(
        "final as (select a.id from a join b using (id)) select a.id from final",
        a_projection="id,\n        customer_id,",
        b_projection="id,\n        amount,",
    )
    compiled = _compiled(
        "final as (select a.id from a join b using (id)) select a.id from final",
        a_projection="id, customer_id,",
        b_projection="id, amount,",
    )
    candidate = _candidate(raw, compiled, dialect="bigquery").decode()
    assert "id,\n        customer_id,\n        amount," in candidate
    assert "b as (" not in candidate


def test_unquoted_unicode_projection_merges():
    final = "final as (select b.café_id from b) select b.café_id from final"
    candidate = _candidate(
        _source(final, a_projection="café_id,\n        client", b_projection="café_id,\n        montant"),
        _compiled(final, a_projection="café_id, client", b_projection="café_id, montant"),
    ).decode()
    assert "café_id,\n        client,\n        montant" in candidate


def test_decode_source_pure_crlf_is_not_mixed():
    decoded = decode_source(b"with a as (\r\nselect x\r\n)\r\nselect x\r\n")
    assert decoded.newline_style == "crlf"


def test_single_line_canonical_needs_insertion_refuses():
    raw = (
        "with a as (\n    select id from {{ ref('stg') }}\n),\n"
        "b as (\n    select id, amount from {{ ref('stg') }}\n)\n"
        "select b.id from b\n"
    )
    compiled = "with a as (select id from db.sch.stg), b as (select id, amount from db.sch.stg) select b.id from b"
    raw_bytes, source, owner, _group, qualified = _qualified(raw, compiled)
    assert qualified.status is FindingStatus.MERGE_ELIGIBLE
    with pytest.raises(RewriteError) as exc_info:
        build_plan(raw_bytes, owner.unique_id, Path("m.sql"), (qualified,), source)
    assert exc_info.value.reason_code is ReasonCode.UNSUPPORTED_IMPORT_SHAPE


def test_single_line_canonical_redundant_donor_merges():
    raw = (
        "with a as (\n    select id, amount from {{ ref('stg') }}\n),\n"
        "b as (\n    select id from {{ ref('stg') }}\n)\n"
        "select b.id from b\n"
    )
    compiled = "with a as (select id, amount from db.sch.stg), b as (select id from db.sch.stg) select b.id from b"
    candidate = _candidate(raw, compiled).decode()
    assert "b as (" not in candidate
    assert "select id, amount" in candidate


def test_last_cte_donor_removes_preceding_separator():
    raw = (
        "with a as (\n"
        "    select\n"
        "        id,\n"
        "        customer_id\n"
        "    from {{ ref('stg') }}\n"
        "),\n"
        "b as (\n"
        "    select\n"
        "        id,\n"
        "        amount\n"
        "    from {{ ref('stg') }}\n"
        ")\n"
        "select a.id, b.amount from a join b on a.id = b.id\n"
    )
    compiled = (
        "with a as (select id, customer_id from db.sch.stg), "
        "b as (select id, amount from db.sch.stg) "
        "select a.id, b.amount from a join b on a.id = b.id"
    )
    candidate = _candidate(raw, compiled).decode()
    assert "b as (" not in candidate
    assert "),\nselect" not in candidate
    assert "join a as b" in candidate
    assert len(parse_source_model(candidate.encode()).ctes) == 1


def test_consecutive_donors_with_last_donor_do_not_overlap_edits():
    raw = (
        "with a as (\n"
        "    select\n"
        "        id,\n"
        "        base_value\n"
        "    from {{ ref('stg') }}\n"
        "),\n"
        "b as (\n"
        "    select\n"
        "        id,\n"
        "        amount\n"
        "    from {{ ref('stg') }}\n"
        "),\n"
        "c as (\n"
        "    select\n"
        "        id,\n"
        "        customer_id\n"
        "    from {{ ref('stg') }}\n"
        ")\n"
        "select b.amount, c.customer_id from b join c using (id)\n"
    )
    compiled = raw.replace("{{ ref('stg') }}", "db.sch.stg")
    candidate = _candidate(raw, compiled).decode()
    assert "b as (" not in candidate
    assert "c as (" not in candidate
    assert "base_value,\n        amount,\n        customer_id" in candidate
    assert "from a as b join a as c using (id)" in candidate
    assert "),\nselect" not in candidate
    assert ")\nselect b.amount" in candidate
    assert len(parse_source_model(candidate.encode()).ctes) == 1


def test_nested_cte_shadow_refuses_end_to_end():
    final = (
        "wrapper as (with b as (select id from dim) select id from b),\n"
        "final as (select a.id from a join b on a.id = b.id)\n"
        "select a.id from final"
    )
    compiled_final = (
        "wrapper as (with b as (select id from dim) select id from b), "
        "final as (select a.id from a join b on a.id = b.id) select a.id from final"
    )
    *_unused, qualified = _qualified(_source(final), _compiled(compiled_final))
    assert qualified.status is FindingStatus.NOT_ELIGIBLE
    assert qualified.reason_codes == (ReasonCode.REFERENCE_BINDING_AMBIGUOUS,)


@pytest.mark.parametrize(
    ("a_where", "b_where"),
    [
        (" where amount > 100", " where amount > 100.0"),
        (" where amount > 1 and id < 2", " where id < 2 and amount > 1"),
    ],
)
def test_semantically_plausible_but_ast_different_predicates_refuse(a_where, b_where):
    final = "select a.id from a join b on a.id = b.id"
    raw = _source(final, a_projection="id", b_projection="id", a_where=a_where, b_where=b_where)
    compiled = _compiled(final, a_projection="id", b_projection="id", a_where=a_where, b_where=b_where)
    *_unused, qualified = _qualified(raw, compiled)
    assert qualified.reason_codes == (ReasonCode.DIFFERENT_PREDICATE,)


def test_compound_refusal_reason_codes_have_stable_order():
    final = "select a.x from a join b on a.x = b.x"
    raw = _source(
        final,
        a_projection="amount as x",
        b_projection="customer_id as x",
        a_where=" where amount > 100",
        b_where=" where amount > 500",
    )
    compiled = _compiled(
        final,
        a_projection="amount as x",
        b_projection="customer_id as x",
        a_where=" where amount > 100",
        b_where=" where amount > 500",
    )
    *_unused, qualified = _qualified(raw, compiled)
    assert qualified.reason_codes == (ReasonCode.DIFFERENT_PREDICATE, ReasonCode.PROJECTION_COLLISION)


def test_duplicate_output_within_single_cte_refuses():
    final = "select a.x from a join b on a.x = b.x"
    raw = _source(final, a_projection="id as x,\n        customer_id as x", b_projection="id as x")
    compiled = _compiled(final, a_projection="id as x, customer_id as x", b_projection="id as x")
    *_unused, qualified = _qualified(raw, compiled)
    assert qualified.reason_codes == (ReasonCode.PROJECTION_COLLISION,)


@pytest.mark.parametrize(
    "source",
    [
        b"with `a` as (select x from {{ ref('stg') }}) select x from `a`",
        b"with [a] as (select x from {{ ref('stg') }}) select x from [a]",
    ],
)
def test_unsupported_cte_delimiters_refuse_cleanly(source):
    with pytest.raises(SourceParseError) as exc_info:
        parse_source_model(source)
    assert exc_info.value.reason_code is ReasonCode.UNSUPPORTED_IMPORT_SHAPE


def test_bom_preserved_through_actual_rewrite():
    final = "final as (select b.id from b) select b.id from final"
    candidate = _candidate(b"\xef\xbb\xbf" + _source(final).encode(), _compiled(final))
    expected = (
        b"\xef\xbb\xbfwith a as (\n"
        b"    select\n"
        b"        id,\n"
        b"        customer_id,\n"
        b"        amount\n"
        b"    from {{ ref('stg') }}\n"
        b"),\n"
        b"final as (select b.id from a as b) select b.id from final\n"
    )
    assert candidate == expected


def test_tab_indented_select_list_insertion():
    raw = (
        "with a as (\n\tselect\n\t\tid,\n\t\tcustomer_id\n\tfrom {{ ref('stg') }}\n),\n"
        "b as (\n\tselect\n\t\tid,\n\t\tamount\n\tfrom {{ ref('stg') }}\n),\n"
        "final as (select b.id from b)\nselect b.id from final\n"
    )
    compiled = _compiled("final as (select b.id from b) select b.id from final")
    candidate = _candidate(raw, compiled)
    assert b"\t\tcustomer_id,\n\t\tamount" in candidate


def test_no_trailing_newline_remains_absent():
    final = "final as (select b.id from b) select b.id from final"
    raw = _source(final, trailing_newline=False)
    candidate = _candidate(raw, _compiled(final))
    expected = (
        b"with a as (\n"
        b"    select\n"
        b"        id,\n"
        b"        customer_id,\n"
        b"        amount\n"
        b"    from {{ ref('stg') }}\n"
        b"),\n"
        b"final as (select b.id from a as b) select b.id from final"
    )
    assert candidate == expected


def test_mixed_crlf_lf_file_preserves_local_bytes():
    final = "final as (select b.id from b)\nselect b.id from final\n"
    raw = (
        "with a as (\r\n"
        "    select\r\n"
        "        id,\r\n"
        "        customer_id\r\n"
        "    from {{ ref('stg') }}\r\n"
        "),\n"
        "b as (\n"
        "    select\n"
        "        id,\n"
        "        amount\n"
        "    from {{ ref('stg') }}\n"
        "),\n"
        f"{final}"
    ).encode()
    compiled = _compiled("final as (select b.id from b) select b.id from final")
    candidate = _candidate(raw, compiled)
    expected = (
        b"with a as (\r\n"
        b"    select\r\n"
        b"        id,\r\n"
        b"        customer_id,\r\n"
        b"        amount\r\n"
        b"    from {{ ref('stg') }}\r\n"
        b"),\n"
        b"final as (select b.id from a as b)\n"
        b"select b.id from final\n"
    )
    assert candidate == expected


def test_cr_only_file_insertion_refuses_cleanly():
    final = "final as (select b.id from b) select b.id from final"
    raw = _source(final).replace("\n", "\r")
    raw_bytes, source, owner, _group, qualified = _qualified(raw, _compiled(final))
    with pytest.raises(RewriteError) as exc_info:
        build_plan(raw_bytes, owner.unique_id, Path("m.sql"), (qualified,), source)
    assert exc_info.value.reason_code is ReasonCode.UNSUPPORTED_IMPORT_SHAPE


def test_quoted_and_unquoted_cte_names_redirect_with_exact_spelling():
    final = 'final as (select "Orders".amount from "Orders") select "Orders".amount from final'
    candidate = _candidate(
        _source(final, a_name="orders", b_name='"Orders"'),
        _compiled(final, a_name="orders", b_name='"Orders"'),
    ).decode()
    assert 'from orders as "Orders"' in candidate


@pytest.mark.parametrize(
    ("dialect", "canonical_column", "donor_column", "expected_count"),
    [
        ("snowflake", "order_id", "Order_ID", 1),
        ("clickhouse", "order_id", "Order_ID", 2),
    ],
)
def test_case_different_outputs_follow_dialect_fold(dialect, canonical_column, donor_column, expected_count):
    final = "select a.order_id from a join b on a.order_id = b.Order_ID"
    raw = _source(final, a_projection=f"id,\n        {canonical_column}", b_projection=f"id,\n        {donor_column}")
    compiled = _compiled(final, a_projection=f"id, {canonical_column}", b_projection=f"id, {donor_column}")
    candidate = _candidate(raw, compiled, dialect=dialect).decode()
    before_main = candidate.split("select a.order_id from", 1)[0]
    assert before_main.count("order_id") + before_main.count("Order_ID") == expected_count


def test_quoted_case_variants_never_fold():
    final = 'select a."OrderID" from a join b on a."OrderID" = b."orderid"'
    raw = _source(final, a_projection='id,\n        "OrderID"', b_projection='id,\n        "orderid"')
    compiled = _compiled(final, a_projection='id, "OrderID"', b_projection='id, "orderid"')
    candidate = _candidate(raw, compiled, dialect="snowflake").decode()
    assert '"OrderID",\n        "orderid"' in candidate


@pytest.mark.parametrize(
    ("dialect", "canonical_alias", "donor_alias"),
    [
        ("postgres", "x", '"x"'),
        ("snowflake", "X", '"X"'),
        ("clickhouse", "X", '"X"'),
    ],
)
def test_quoted_and_unquoted_same_identity_collide(dialect, canonical_alias, donor_alias):
    final = "select a.x from a join b on true"
    raw = _source(
        final,
        a_projection=f"amount as {canonical_alias}",
        b_projection=f"customer_id as {donor_alias}",
    )
    compiled = _compiled(
        final,
        a_projection=f"amount as {canonical_alias}",
        b_projection=f"customer_id as {donor_alias}",
    )
    *_unused, qualified = _qualified(raw, compiled, dialect=dialect)
    assert qualified.reason_codes == (ReasonCode.PROJECTION_COLLISION,)


@pytest.mark.parametrize("dialect", ["postgres", "snowflake"])
def test_unquoted_case_variants_collide_under_folding_dialects(dialect):
    final = "select a.Key from a join b on true"
    raw = _source(final, a_projection="amount as Key", b_projection="customer_id as key")
    compiled = _compiled(final, a_projection="amount as Key", b_projection="customer_id as key")
    *_unused, qualified = _qualified(raw, compiled, dialect=dialect)
    assert qualified.reason_codes == (ReasonCode.PROJECTION_COLLISION,)


def test_unicode_prefix_and_quoted_identifier_spans_are_byte_exact():
    final = 'final as (select donneur."café_id" from donneur) select donneur."café_id" from final'
    raw = "-- π\n" + _source(
        final,
        a_name="café",
        b_name="donneur",
        a_projection='"café_id",\n        client',
        b_projection='"café_id",\n        montant',
    )
    compiled = _compiled(
        final,
        a_name="café",
        b_name="donneur",
        a_projection='"café_id", client',
        b_projection='"café_id", montant',
    )
    candidate = _candidate(raw, compiled)
    expected = (
        "-- π\n"
        "with café as (\n"
        "    select\n"
        '        "café_id",\n'
        "        client,\n"
        "        montant\n"
        "    from {{ ref('stg') }}\n"
        "),\n"
        'final as (select donneur."café_id" from café as donneur) '
        'select donneur."café_id" from final\n'
    ).encode()
    assert candidate == expected


def test_backtick_projection_duplicates_report_unsupported_shape():
    raw = (
        b"with a as (select `order id` from {{ ref('stg') }}), b as (select `order id` from {{ ref('stg') }}) select 1"
    )
    finding = detect_source_duplicates(raw, None, "model.p.m", Path("m.sql"))
    assert finding is not None
    assert finding.status is FindingStatus.NEEDS_COMPILED_ANALYSIS
    assert finding.reason_codes == (ReasonCode.UNSUPPORTED_IMPORT_SHAPE,)


def test_donors_separated_by_unrelated_cte_preserve_middle_block():
    raw = (
        "with a as (\n"
        "    select\n"
        "        id,\n"
        "        customer_id\n"
        "    from {{ ref('stg') }}\n"
        "),\n"
        "unrelated as (\n    select 42 as answer\n),\n"
        "b as (\n"
        "    select\n"
        "        id,\n"
        "        amount\n"
        "    from {{ ref('stg') }}\n"
        "),\n"
        "final as (select b.id from b)\n"
        "select b.id from final\n"
    )
    compiled = (
        "with a as (select id, customer_id from db.sch.stg), "
        "unrelated as (select 42 as answer), "
        "b as (select id, amount from db.sch.stg), "
        "final as (select b.id from b) select b.id from final"
    )
    middle = "unrelated as (\n    select 42 as answer\n),"
    candidate = _candidate(raw, compiled).decode()
    assert middle in candidate
    assert "b as (" not in candidate


def test_four_members_one_divergent_filter_refuses_whole_group():
    raw = (
        "with a as (select id from {{ ref('stg') }} where status = 'active'), "
        "b as (select id from {{ ref('stg') }} where status = 'active'), "
        "c as (select id from {{ ref('stg') }} where status = 'active'), "
        "d as (select id from {{ ref('stg') }} where status = 'pending') "
        "select a.id from a join b using (id) join c using (id) join d using (id)"
    )
    compiled = raw.replace("{{ ref('stg') }}", "db.sch.stg")
    raw_bytes = raw.encode()
    view, owner = _manifest_for_refs()
    source = parse_source_model(raw_bytes)
    matched = match_source_ctes(
        tuple(cte for cte in source.ctes if cte.ref_call is not None),
        parse_model(compiled),
        view,
        owner,
    )
    groups = group_imports(list(matched), owner.unique_id)
    assert len(groups) == 1
    qualified = qualify_group(groups[0], downstream_refs=source.downstream_refs)
    assert len(groups[0].imports) == 4
    assert qualified.reason_codes == (ReasonCode.DIFFERENT_PREDICATE,)


def test_bare_and_aliased_donor_self_join_redirects_every_binding():
    final = "final as (select b.id, rhs.amount from b join b as rhs using (id)) select * from final"
    candidate = _candidate(_source(final), _compiled(final)).decode()
    assert "from a as b join a as rhs using (id)" in candidate


def test_mixed_alias_styles_redirect_independently():
    raw = (
        "with a as (select id from {{ ref('stg') }}), "
        "b as (select id from {{ ref('stg') }}), "
        "c as (select id from {{ ref('stg') }}), "
        "d as (select id from {{ ref('stg') }}), "
        "final as (select b.id from b join c as cc on b.id = cc.id join d dd on b.id = dd.id) "
        "select b.id from final"
    )
    compiled = raw.replace("{{ ref('stg') }}", "db.sch.stg")
    raw_bytes = raw.encode()
    view, owner = _manifest_for_refs()
    source = parse_source_model(raw_bytes)
    matched = match_source_ctes(
        tuple(cte for cte in source.ctes if cte.ref_call is not None),
        parse_model(compiled),
        view,
        owner,
    )
    group = group_imports(list(matched), owner.unique_id)[0]
    qualified = qualify_group(group, downstream_refs=source.downstream_refs)
    plan = build_plan(raw_bytes, owner.unique_id, Path("m.sql"), (qualified,), source)
    candidate = apply_edits(raw_bytes, plan.edits).decode()
    assert "from a as b" in candidate
    assert "join a as cc" in candidate
    assert "join a dd" in candidate


def test_two_independent_groups_rewrite_in_one_fast_plan():
    raw = (
        "with a as (select id from {{ ref('stg') }}), "
        "b as (select id from {{ ref('stg') }}), "
        "c as (select customer_id from {{ ref('customers') }}), "
        "d as (select customer_id from {{ ref('customers') }}), "
        "final as (select b.id, d.customer_id from b join d on true) "
        "select b.id from final"
    )
    compiled = (
        "with a as (select id from db.sch.stg), b as (select id from db.sch.stg), "
        "c as (select customer_id from db.sch.customers), d as (select customer_id from db.sch.customers), "
        "final as (select b.id, d.customer_id from b join d on true) select b.id from final"
    )
    view_owner = _manifest_for_two_refs()
    raw_bytes = raw.encode()
    source = parse_source_model(raw_bytes)
    view, owner = view_owner
    matched = match_source_ctes(
        tuple(cte for cte in source.ctes if cte.ref_call is not None),
        parse_model(compiled),
        view,
        owner,
    )
    groups = group_imports(list(matched), owner.unique_id)
    qualified = tuple(qualify_group(group, downstream_refs=source.downstream_refs) for group in groups)
    plan = build_plan(raw_bytes, owner.unique_id, Path("m.sql"), qualified, source)
    candidate = apply_edits(raw_bytes, plan.edits).decode()
    assert len(groups) == 2
    assert "b as (" not in candidate
    assert "d as (" not in candidate
    assert "from a as b join c as d" in candidate


def test_nondeterministic_downstream_refuses_merge():
    final = "select random(), b.id from b"
    raw = _source(final)
    compiled = _compiled(final)
    parsed = parse_model(compiled)
    *_unused, qualified = _qualified(raw, compiled, whole_model_ok=analyze_volatility(parsed).ok)
    assert qualified.reason_codes == (ReasonCode.NONDETERMINISTIC,)


def test_unreferenced_dead_donor_is_removed_without_redirect():
    final = "select a.id from a"
    candidate = _candidate(_source(final), _compiled(final)).decode()
    assert "b as (" not in candidate
    assert "customer_id,\n        amount" in candidate
    assert " as b" not in candidate


def test_comment_inside_donor_select_refuses_cleanly():
    final = "select b.id from b"
    raw = _source(final, b_projection="id,\n        amount /* donor meaning */ as amount")
    compiled = _compiled(final, b_projection="id, amount as amount")
    raw_bytes, source, owner, _group, qualified = _qualified(raw, compiled)
    with pytest.raises(RewriteError) as exc_info:
        build_plan(raw_bytes, owner.unique_id, Path("m.sql"), (qualified,), source)
    assert exc_info.value.reason_code is ReasonCode.COMMENT_RELOCATION_UNSUPPORTED


def test_group_members_reading_different_relations_refuse():
    # S7: both CTEs ref('stg') in source, but the compiled SQL reads two different relations.
    final = "final as (select a.customer_id, b.amount from a join b using (id))\nselect * from final"
    compiled = (
        "with a as (select id, customer_id from db.sch.stg), "
        "b as (select id, amount from db.other.unrelated), "
        "final as (select a.customer_id, b.amount from a join b using (id)) select * from final"
    )
    *_unused, qualified = _qualified(_source(final), compiled)
    assert qualified.status is FindingStatus.NOT_ELIGIBLE
    assert qualified.reason_codes == (ReasonCode.SOURCE_MAPPING_AMBIGUOUS,)


def test_build_plan_refuses_ineligible_group():
    # S8: the planner must not merge a group qualification rejected, even if a caller passes it.
    final = "final as (select a.customer_id, b.amount from a join b using (id))\nselect * from final"
    raw = _source(final, b_where=" where amount > 500")
    compiled = _compiled(final, b_where=" where amount > 500")
    raw_bytes, source, owner, _group, qualified = _qualified(raw, compiled)
    assert qualified.reason_codes == (ReasonCode.DIFFERENT_PREDICATE,)
    with pytest.raises(RewriteError) as exc_info:
        build_plan(raw_bytes, owner.unique_id, Path("m.sql"), (qualified,), source)
    assert exc_info.value.reason_code is ReasonCode.DIFFERENT_PREDICATE
