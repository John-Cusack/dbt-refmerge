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


@pytest.mark.parametrize(
    "sql",
    [
        "",
        "select 1",
        "values (1)",
        "{{ config(materialized='table') }}",
        "select * from (with a as (select 1) select * from a) s",
    ],
    ids=["empty", "select", "values", "jinja-only", "with-after-select"],
)
def test_source_without_with_has_no_ctes(sql):
    model = parse_source_model(_src(sql))
    assert (model.ctes, model.downstream_refs, model.nested_names) == ((), (), frozenset())


@pytest.mark.parametrize(
    "sql",
    [
        "with recursive a as (select 1) select 1",
        "with a as (select 1), recursive b as (select 1) select 1",
        "with a as (select 1), A as (select 1) select 1",
        'with "a" as (select 1), a as (select 1) select 1',
        "with a x as (select 1) select 1",
        "with a (x) as (select 1) select 1",
        "with a as not materialized (select 1) select 1",
        "with a as materialized (select 1) select 1",
        "with a as select 1",
        "with a",
        "with a as (select (1) select 1",
        "with a as (select 1), , b as (select 1) select 1",
        "with 'a' as (select 1) select 1",
    ],
    ids=[
        "recursive",
        "recursive-after-comma",
        "duplicate-folded",
        "duplicate-quoted",
        "name-then-word",
        "column-list",
        "not-materialized",
        "materialized",
        "missing-parens",
        "name-at-end",
        "unbalanced",
        "empty-list-item",
        "single-quoted-name",
    ],
)
def test_cte_list_syntax_refusals(sql):
    with pytest.raises(SourceParseError) as exc_info:
        parse_source_model(_src(sql))
    assert exc_info.value.reason_code is ReasonCode.UNSUPPORTED_IMPORT_SHAPE


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("with {{ all_ctes() }} select 1", []),
        ("with a as (select 1), {{ more_ctes() }} select 1", [("a", ",")]),
        ("with a as (select 1), {{ more_ctes_and_query() }}", [("a", ",")]),
        ('with a as (select 1),\n"B" as (select 2)\nselect 1', [("a", ","), ('"B"', None)]),
    ],
    ids=["whole-list", "rest-of-list", "rest-of-model", "no-jinja"],
)
def test_cte_list_ends_at_main_query_or_end_of_source(sql, expected):
    # CTEs rendered by masked Jinja are invisible, so the parsed list simply stops there.
    raw = _src(sql)
    model = parse_source_model(raw)
    assert [
        (
            cte.identifier.source_text,
            None
            if cte.separator_span is None
            else raw[cte.separator_span.start_byte : cte.separator_span.end_byte].decode(),
        )
        for cte in model.ctes
    ] == expected


_REF = "{{ ref('m') }}"


def test_backtick_identifiers_preserve_source_and_decode_doubled_delimiters():
    raw = _src(f"with `Order Imports` as (select `Order ID` as `x``y` from {_REF}) select `x``y` from `Order Imports`")
    model = parse_source_model(raw)
    (cte,) = model.ctes
    assert (cte.identifier.source_text, cte.identifier.value) == ("`Order Imports`", "Order Imports")
    assert cte.ref_call is not None
    assert cte.projections[0].upstream_identifier.value == "Order ID"
    assert cte.projections[0].output_identifier.value == "x`y"
    assert model.downstream_refs[0].cte_identity == cte.identifier.identity


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        # Supported: (projections as (upstream, output, attached comments), select list, predicate).
        (f"select id from {_REF}", ([("id", "id", [])], "id", None)),
        (f"select `id` from {_REF}", ([("`id`", "`id`", [])], "`id`", None)),
        (
            f'select id as ident, "Amount" amt, "x""y" from {_REF}',
            (
                [("id", "ident", []), ('"Amount"', "amt", []), ('"x""y"', '"x""y"', [])],
                'id as ident, "Amount" amt, "x""y"',
                None,
            ),
        ),
        (
            f"select\n  id,\n  amount,\nfrom {_REF}",
            ([("id", "id", []), ("amount", "amount", [])], "id,\n  amount,", None),
        ),
        (
            f"select amount /* euros */ as amt from {_REF} where id > 0 and start = 1",
            ([("amount", "amt", ["/* euros */"])], "amount /* euros */ as amt", "where id > 0 and start = 1"),
        ),
        (f"select id -- trailing\nfrom {_REF}\nwhere", ([("id", "id", [])], "id", "where")),
        # Not an import: ref_call None, no projections, the whole body as the select list.
        ("", None),
        ("values (1)", None),
        ("select 1", None),
        ("select id from", None),
        (f"select from {_REF}", None),
        (f"select distinct id from {_REF}", None),
        (f"select all id from {_REF}", None),
        ("select id from tbl", None),
        ("select id from {{ var('relation') }}", None),
        (f"select id from {_REF} as t", None),
        (f"select id from {_REF} t where id > 0", None),
        (f"select * from {_REF}", None),
        (f"select t.id from {_REF}", None),
        (f"select lower(id) from {_REF}", None),
        (f"select 'x' as id from {_REF}", None),
        (f"select id,, amount from {_REF}", None),
        (f"select , id from {_REF}", None),
        (f"select id /* key */, amount from {_REF}", None),
        (f"select (select 1 from t) as id from {_REF}", None),
    ],
)
def test_import_body_shapes(body, expected):
    raw = _src(f"with a as ({body}) select 1")
    (cte,) = parse_source_model(raw).ctes

    def text(span):
        return None if span is None else raw[span.start_byte : span.end_byte].decode()

    actual = (
        [
            (
                p.upstream_identifier.source_text,
                p.output_identifier.source_text,
                [text(c) for c in p.attached_comment_spans],
            )
            for p in cte.projections
        ],
        text(cte.select_list_span),
        text(cte.predicate_source_span),
    )
    if expected is None:
        assert (cte.ref_call, actual) == (None, ([], body, None))
    else:
        assert cte.ref_call is not None
        assert actual == expected


def test_scan_findings_point_at_the_first_duplicated_import():
    raw = b"-- header\nwith other as (select 1 as x),\n\na as (select id from {{ ref('m') }}),\nb as (select id from {{ ref('m') }})\nselect 1"
    unsupported = raw.replace(b"select id from {{ ref('m') }}),\nb", b"select distinct id from {{ ref('m') }}),\nb")

    assert detect_source_duplicates(raw, None, "model.p.m", Path("m.sql")).line == 4
    assert detect_source_duplicates(unsupported, None, "model.p.m", Path("m.sql")).line == 4


@pytest.mark.parametrize(
    ("raw", "line"),
    [
        (b"with recursive a as (select id from {{ ref('m') }}),\nb as (select id from {{ ref('m') }}) select 1", 1),
        (b"with a (id) as (\nselect id from {{ source('s', 't') }}), b as (select id from {{ source('s', 't') }})", 2),
        (b"with recursive a as (select id from {{ ref('m') }}), b as (select id from {{ ref('n') }}) select 1", None),
        (b"with a as (select __r0__ from {{ ref('m') }}),\nb as (select id from {{ ref('m') }}) select 1", 1),
        (b"with recursive a as (select id from {{ ref('m') }}), b as (select id from {{ ref('m') ", None),
        (b"with recursive a as (select id from {{ ref('m') }}), b as (select id from {{ ref('m') }}) \xff", None),
    ],
    ids=[
        "recursive-duplicate",
        "column-list-duplicate",
        "recursive-distinct",
        "sentinel-name-duplicate",
        "unterminated-jinja",
        "invalid-utf8",
    ],
)
def test_scan_reports_models_the_cte_parser_refuses_only_when_a_relation_is_named_twice(raw, line):
    finding = detect_source_duplicates(raw, None, "model.p.m", Path("m.sql"))

    if line is None:
        assert finding is None
    else:
        assert (finding.status, finding.reason_codes, finding.cte_names, finding.line) == (
            FindingStatus.NEEDS_COMPILED_ANALYSIS,
            (ReasonCode.UNSUPPORTED_IMPORT_SHAPE,),
            (),
            line,
        )
