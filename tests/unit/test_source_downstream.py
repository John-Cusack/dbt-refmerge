"""Downstream CTE references and scan-time duplicate detection in the source frontend."""

from pathlib import Path

import pytest

from dbt_refmerge.analyze import FindingStatus
from dbt_refmerge.domain import ReasonCode
from dbt_refmerge.orchestrator import detect_source_duplicates
from dbt_refmerge.source import has_unsupported_duplicate_candidates, parse_source_model

_IMPORTS = "with a as (select id from {{ ref('m') }}), b as (select id from {{ ref('m') }}), "


def _refs(tail: str) -> list[tuple[str, str, bool, bool]]:
    """(identity, source bytes, has_alias, star_expansion) for each downstream reference."""
    raw = (_IMPORTS + tail).encode()
    model = parse_source_model(raw)
    return [
        (
            ref.cte_identity.value,
            raw[ref.span.start_byte : ref.span.end_byte].decode(),
            ref.has_alias,
            ref.star_expansion,
        )
        for ref in model.downstream_refs
    ]


@pytest.mark.parametrize(
    "final",
    [
        "final as (select x.* from b as `x`) select 1",
        "final as (select `x`.* from b as x) select 1",
        "final as (select x.* from b as) select 1",
    ],
    ids=["backtick-alias", "backtick-qualifier", "missing-alias"],
)
def test_qualified_star_through_unreadable_alias_counts_as_star_expansion(final):
    # An alias the frontend cannot resolve may be the qualifier of any `q.*` in the scope.
    assert _refs(final) == [("b", "b", True, True)]


def _ref_flags(tail: str) -> list[tuple[str, str, bool, str | None, bool, bool, bool]]:
    """(identity, span bytes, has_alias, context, binding_ambiguous, star_expansion, multi_relation)."""
    raw = (_IMPORTS + tail).encode()
    return [
        (
            ref.cte_identity.value,
            raw[ref.span.start_byte : ref.span.end_byte].decode(),
            ref.has_alias,
            ref.context_cte,
            ref.binding_ambiguous,
            ref.star_expansion,
            ref.multi_relation,
        )
        for ref in parse_source_model(raw).downstream_refs
    ]


@pytest.mark.parametrize(
    ("tail", "expected"),
    [
        # A keyword after the relation is not an alias.
        (
            "final as (select b.id from b cross join t) select 1",
            [("b", "b", False, "final", False, False, True)],
        ),
        ('final as (select "X".id from b "X") select 1', [("b", "b", True, "final", False, False, False)]),
        ('final as (select "B".id from "B") select 1', []),
        ('final as (select 1 from "b" as x) select 1', [("b", '"b"', True, "final", False, False, False)]),
        # Only the star qualified by the reference's own alias counts.
        (
            "final as (select t.*, b.id from b join t on true) select 1",
            [("b", "b", False, "final", False, False, True)],
        ),
        (
            "final as (select x.* from t join b as x on true) select 1",
            [("b", "b", True, "final", False, True, True)],
        ),
        ("select * from b", [("b", "b", False, None, False, True, False)]),
        # A masked relation still joins, so the scope has several relations.
        (
            "final as (select x.id from {{ var('rel') }} x join b on true) select 1",
            [("b", "b", False, "final", False, False, True)],
        ),
        # An import body names no relation but its ref, even inside a predicate subquery.
        (
            "c as (select id from {{ ref('n') }} where id in (select id from a, b)) select 1",
            [],
        ),
        # Comma joins: the relation after FROM binds normally, one after a comma is ambiguous.
        (
            "final as (select b.id from b, raw_table) select 1",
            [("b", "b", False, "final", False, False, True)],
        ),
        (
            "final as (select b.id from raw_table, b as q where q.id > 0) select 1",
            [("b", "b", True, "final", True, False, True)],
        ),
        (
            "select s.id from (select a.id from x, a) s, b",
            [("a", "a", False, None, True, False, True), ("b", "b", False, None, True, False, True)],
        ),
        ("select 1 from a, ", [("a", "a", False, None, False, False, True)]),
        ("select a.id, (select 1), b.id from a", [("a", "a", False, None, False, False, False)]),
        (
            "final as (select a.id from a natural join b) select 1",
            [("a", "a", False, "final", True, False, True), ("b", "b", False, "final", True, False, True)],
        ),
    ],
    ids=[
        "keyword-after-relation",
        "quoted-implicit-alias",
        "quoted-case-differs",
        "quoted-relation",
        "other-qualified-star",
        "own-qualified-star",
        "bare-star",
        "jinja-relation",
        "subquery-in-import",
        "comma-after-cte",
        "comma-before-cte",
        "comma-in-subquery-and-outer",
        "trailing-comma",
        "select-list-commas",
        "natural-join",
    ],
)
def test_downstream_ref_edge_shapes(tail, expected):
    assert _ref_flags(tail) == expected


def test_downstream_ref_records_unqualified_identities_of_its_scope():
    raw = (_IMPORTS + 'final as (select b.id, amount, "Note" from b join t on t.id = b.id) select 1').encode()
    (ref,) = parse_source_model(raw).downstream_refs
    assert ref.unqualified_identities == frozenset({"select", "amount", "Note", "from", "b", "join", "t", "on"})


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        (_IMPORTS + "final as (select 1) select 1", False),
        ("with a as (select id from {{ ref('m') }}), b as (select distinct id from {{ ref('m') }}) select 1", True),
        ("with a as (select id from {{ ref('m') }}), b as (select distinct id from {{ ref('n') }}) select 1", False),
        (
            "with a as (select id from {{ ref('m') }}), "
            "b as (select id from {{ ref('m') }} join {{ ref('m') }} using (id)) select 1",
            False,
        ),
        ("with a as (select id from {{ ref('m') }}), b as (select 1) select 1", False),
        ("with a as (select * from {{ ref('m') }}), b as (select * from {{ ref('m') }}) select 1", True),
    ],
    ids=[
        "all-supported",
        "one-unsupported",
        "different-upstreams",
        "two-calls-in-unsupported",
        "no-call-in-unsupported",
        "all-unsupported",
    ],
)
def test_has_unsupported_duplicate_candidates(sql, expected):
    assert has_unsupported_duplicate_candidates(parse_source_model(sql.encode())) is expected


def test_scan_mixed_supported_and_unsupported_duplicate_imports():
    raw = b"with a as (select id from {{ ref('m') }}), b as (select distinct id from {{ ref('m') }}) select 1"
    finding = detect_source_duplicates(raw, None, "model.p.m", Path("m.sql"))
    assert finding is not None
    assert (finding.status, finding.cte_names, finding.reason_codes) == (
        FindingStatus.NEEDS_COMPILED_ANALYSIS,
        (),
        (ReasonCode.UNSUPPORTED_IMPORT_SHAPE,),
    )
