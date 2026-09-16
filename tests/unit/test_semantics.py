"""Semantics: allowlist, fingerprints, volatility."""

import pytest

from dbt_refmerge.domain import ReasonCode
from dbt_refmerge.semantics import (
    analyze_volatility,
    parse_model,
    predicate_fingerprint,
    qualify_import_cte,
)


def test_direct_import_qualifies():
    p = parse_model("with a as (select x, y from sch.tbl) select * from a", "postgres")
    assert qualify_import_cte(p.ctes["a"]).ok


def test_join_rejected():
    p = parse_model("with a as (select x from t1 join t2 on true) select * from a", "postgres")
    assert not qualify_import_cte(p.ctes["a"]).ok


def test_predicate_fingerprint_stable():
    p1 = parse_model("with a as (select x from t where x > 1) select * from a", "postgres")
    p2 = parse_model("with a as (select x from t WHERE x>1) select * from a", "postgres")
    assert predicate_fingerprint(p1.ctes["a"]) == predicate_fingerprint(p2.ctes["a"])


def test_absent_predicate_none():
    p = parse_model("with a as (select x from t) select * from a", "postgres")
    assert predicate_fingerprint(p.ctes["a"]) is None


def test_volatile_blocked():
    p = parse_model("with a as (select x from t) select random() from a", "postgres")
    assert not analyze_volatility(p).ok


def test_unordered_limit_blocked():
    p = parse_model("with a as (select x from t) select * from a limit 5", "postgres")
    assert not analyze_volatility(p).ok


def _volatility(final: str):
    return analyze_volatility(parse_model(f"with a as (select id from t) {final}", "postgres"))


@pytest.mark.parametrize(
    "final",
    [
        "select id from a tablesample system (10)",
        "select id from a fetch first 5 rows only",
        "select id from a offset 5",
        "select distinct on (id) id from a",
        "select * from (select id from a limit 1) s order by id",
        "select id from a union all select id from a limit 3",
    ],
    ids=["tablesample", "fetch-first", "offset", "distinct-on", "subquery-limit", "union-limit"],
)
def test_volatility_refuses_unordered_row_selection(final):
    # S1: every query level that picks a subset of rows needs its own ORDER BY.
    result = _volatility(final)
    assert (result.ok, result.reason_codes) == (False, (ReasonCode.NONDETERMINISTIC,))


@pytest.mark.parametrize(
    "final",
    [
        "select id from a order by id limit 5",
        "select id from a order by id fetch first 5 rows only",
        "select id from a order by id offset 5",
        "select distinct on (id) id from a order by id",
        "select * from (select id from a order by id limit 1) s",
    ],
    ids=["limit", "fetch-first", "offset", "distinct-on", "subquery-limit"],
)
def test_volatility_accepts_ordered_row_selection(final):
    assert _volatility(final).ok
