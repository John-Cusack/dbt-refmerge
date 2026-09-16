"""CTE splitter + import parser."""

import pytest

from dbt_refmerge.errors import SourceParseError
from dbt_refmerge.source import parse_source_model


def _src(sql: str) -> bytes:
    return sql.encode()


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
