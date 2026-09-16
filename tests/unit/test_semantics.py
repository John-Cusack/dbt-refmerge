"""Semantics: allowlist, fingerprints, volatility."""

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
