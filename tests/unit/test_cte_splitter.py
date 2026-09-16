"""CTE splitter + import parser."""

from pathlib import Path

import pytest

from dbt_refmerge.analyze import FindingStatus
from dbt_refmerge.domain import ReasonCode
from dbt_refmerge.errors import SourceParseError
from dbt_refmerge.orchestrator import detect_source_duplicates
from dbt_refmerge.source import parse_source_model


def _src(sql: str) -> bytes:
    return sql.encode()


def _import(body: str):
    (cte,) = parse_source_model(_src(f"with a as ({body}) select 1")).ctes
    return cte


_CLAUSES_AFTER_FROM = [
    "group by x",
    "having x > 1",
    "window w as (partition by x)",
    "qualify x = 1",
    "order by x",
    "limit 5",
    "offset 5",
    "fetch first 5 rows only",
    "union all select x from t",
    "intersect select x from t",
    "except select x from t",
    "minus select x from t",
    "for update",
    "into y",
    "connect by prior x = y",
    "start with x = 1",
    "cluster by x",
    "distribute by x",
    "sort by x",
    "using sample 10",
]


@pytest.mark.parametrize("clause", _CLAUSES_AFTER_FROM)
def test_clause_after_where_refuses_import_as_it_does_without_where(clause):
    without_where = _import(f"select x from {{{{ ref('m') }}}} {clause}")
    with_where = _import(f"select x from {{{{ ref('m') }}}} where x > 0 {clause}")
    assert (without_where.ref_call, with_where.ref_call) == (None, None)


@pytest.mark.parametrize("predicate", ["start > 0", "sort = 'asc' and cluster = 1", '"order" = 1'])
def test_clause_words_used_as_predicate_operands_keep_the_import(predicate):
    cte = _import(f"select x from {{{{ ref('m') }}}} where {predicate}")
    assert cte.ref_call is not None


def test_scan_reports_where_group_by_duplicate_as_unsupported_shape():
    raw = _src(
        "with a as (select x from {{ ref('m') }} where x > 0 group by x),\n"
        "b as (select x from {{ ref('m') }})\n"
        "select * from a join b using (x)"
    )
    finding = detect_source_duplicates(raw, None, "model.p.m", Path("m.sql"))
    assert finding is not None
    assert (finding.status, finding.reason_codes) == (
        FindingStatus.NEEDS_COMPILED_ANALYSIS,
        (ReasonCode.UNSUPPORTED_IMPORT_SHAPE,),
    )


@pytest.mark.parametrize("projection", ["1 x", "x + y", "x::int", "-x", "x = 1", "x || y", "x y z", "x as"])
def test_expression_projection_is_not_an_import_column(projection):
    # Only `col`, `col alias` and `col AS alias` are import columns. Anything else used to be
    # read as an alias (`x + y` became `x AS y`, `1 x` became `x`).
    cte = _import(f"select {projection} from {{{{ ref('m') }}}}")
    assert (cte.ref_call, cte.projections) == (None, ())


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("select cast(x as timestamp with time zone) as t from b", frozenset()),
        ("with b as (select 1 as id) select id from b", frozenset({"b"})),
        ("with recursive b as (select 1 as id) select id from b", frozenset({"b"})),
        ("with x as (select 1 as id), b as (select 2 as id) select id from b", frozenset({"x", "b"})),
        ("with b (id) as (select 1) select id from b", frozenset({"b"})),
        ("with b as not materialized (select 1 as id) select id from b", frozenset({"b"})),
        ('with "B" as materialized (select 1 as id) select id from "B"', frozenset({"B"})),
    ],
    ids=["time-zone", "single", "recursive", "second-in-list", "column-list", "not-materialized", "materialized"],
)
def test_nested_cte_names_collect_every_nested_declaration(body, expected):
    model = parse_source_model(
        _src(f"with a as (select id from {{{{ ref('m') }}}}), wrapper as ({body}) select * from wrapper")
    )
    assert model.nested_names == expected


def test_nested_cte_in_main_query_is_collected():
    model = parse_source_model(
        _src(
            "with a as (select id from {{ ref('m') }}) select s.id from (with a as (select 2 as id) select id from a) s"
        )
    )
    assert model.nested_names == frozenset({"a"})


@pytest.mark.parametrize(
    "nested",
    [
        "with {{ name }} as (select 1 as id) select id from b",
        "with x as (select 1 as id), {{ name }} as (select 2 as id) select id from b",
        "with recursive {{ name }} as (select 1 as id) select id from b",
    ],
)
def test_nested_cte_with_hidden_name_refuses(nested):
    # A Jinja-rendered nested CTE name could shadow any top-level CTE.
    with pytest.raises(SourceParseError) as exc_info:
        parse_source_model(
            _src(f"with b as (select id from {{{{ ref('m') }}}}), wrapper as ({nested}) select * from wrapper")
        )
    assert exc_info.value.reason_code is ReasonCode.UNSUPPORTED_IMPORT_SHAPE


def test_two_ctes_split():
    m = parse_source_model(
        _src("with a as (select x from {{ ref('m') }}), b as (select y from {{ ref('m') }}) select * from a")
    )
    assert [c.identifier.source_text for c in m.ctes] == ["a", "b"]
    assert all(c.ref_call is not None for c in m.ctes)


def test_recursive_rejected():
    with pytest.raises(SourceParseError):
        parse_source_model(_src("with recursive a as (select 1) select * from a"))


def test_column_list_rejected():
    with pytest.raises(SourceParseError):
        parse_source_model(_src("with a(x) as (select x from {{ ref('m') }}) select * from a"))


def test_star_import_marked_unsupported():
    m = parse_source_model(_src("with a as (select * from {{ ref('m') }}) select * from a"))
    assert m.ctes[0].ref_call is None


def test_downstream_alias_tracking():
    m = parse_source_model(
        _src(
            "with a as (select x from {{ ref('m') }}), b as (select x from {{ ref('m') }}) select * from a join b on a.x = b.x"
        )
    )
    by_ident = {}
    for r in m.downstream_refs:
        by_ident.setdefault(r.cte_identity.value, []).append(r.has_alias)
    assert "a" in by_ident and "b" in by_ident
