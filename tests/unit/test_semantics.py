"""Semantics: allowlist, fingerprints, volatility."""

import pytest

from dbt_refmerge.domain import ReasonCode
from dbt_refmerge.semantics import (
    DETERMINISTIC_FUNCTIONS,
    ORDER_SENSITIVE_FUNCTIONS,
    VOLATILE_FUNCTIONS,
    VolatilityResult,
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


@pytest.mark.parametrize(
    "expression",
    [
        # math
        "abs(id)",
        "ceil(id)",
        "ceiling(id)",
        "floor(id)",
        "round(id, 2)",
        "power(id, 2)",
        "id ^ 2",
        "sqrt(id)",
        "exp(id)",
        "ln(id)",
        "log(id)",
        # null handling, conditionals, boolean connectives
        "coalesce(id, 0)",
        "nullif(id, 0)",
        "greatest(id, 1)",
        "least(id, 1)",
        "case when id > 1 then 1 else 0 end",
        "case id when 1 then 2 end",
        "id > 1 and id < 5 or id = 9",
        # strings
        "lower(name)",
        "upper(name)",
        "trim(name)",
        "ltrim(name)",
        "rtrim(name)",
        "substring(name, 1, 2)",
        "substr(name, 1, 2)",
        "replace(name, 'a', 'b')",
        "concat(name, 'x')",
        "concat_ws(',', name, 'x')",
        "length(name)",
        "char_length(name)",
        "strpos(name, 'a')",
        "split_part(name, ',', 1)",
        # conversion and dates
        "to_char(created_at, 'YYYY')",
        "to_number(name, '999')",
        "to_timestamp(name, 'YYYY')",
        "to_timestamp(id)",
        "to_date(name, 'YYYY')",
        "date_part('year', created_at)",
        "date_trunc('day', created_at)",
        "extract(year from created_at)",
        "cast(id as text)",
        "id::text",
        # order-insensitive aggregates, including windows whose frame keeps every peer row
        "count(*)",
        "count(distinct id)",
        "sum(id)",
        "min(id)",
        "max(id)",
        "avg(id)",
        "coalesce(sum(id), 0)",
        "sum(id) over (partition by name)",
        "sum(id) over (order by created_at)",
        "sum(id) over (order by created_at range between unbounded preceding and current row)",
        # STABLE: one value for the whole statement that compares baseline and candidate
        "now()",
        "current_timestamp",
        "current_date",
        "current_time",
    ],
)
def test_volatility_accepts_deterministic_and_stable_functions(expression):
    assert _volatility(f"select {expression} from a") == VolatilityResult(
        ok=True, reason_codes=(ReasonCode.OK,), unknown_functions=()
    )


@pytest.mark.parametrize(
    ("expression", "reported"),
    [
        # volatile, including spellings sqlglot normalizes (random -> rand, gen_random_uuid -> uuid)
        ("random()", "rand"),
        ("coalesce(random(), 0)", "rand"),
        ("gen_random_uuid()", "uuid"),
        ("clock_timestamp()", "clock_timestamp"),
        ("nextval('seq')", "nextval"),
        # order-sensitive (string_agg -> group_concat)
        ("row_number() over (order by id)", "row_number"),
        ("lag(id) over (order by id)", "lag"),
        ("array_agg(id)", "array_agg"),
        ("string_agg(name, ',')", "group_concat"),
        # a ROWS frame counts physical rows, so ties make any aggregate over it order-dependent
        ("sum(id) over (order by created_at rows between 1 preceding and current row)", "window_frame"),
        ("count(*) over (partition by name rows unbounded preceding)", "window_frame"),
        ("sum(id) over w from a window w as (order by created_at rows 2 preceding)", "window_frame"),
        # unknown: user-defined, schema-qualified (never the builtin) or simply not allowlisted
        ("my_udf(id)", "my_udf"),
        ("coalesce(my_udf(id), 0)", "my_udf"),
        ("analytics.lower(name)", "lower"),
        ("any_value(id)", "any_value"),
    ],
)
def test_volatility_refuses_volatile_order_sensitive_and_unknown_functions(expression, reported):
    final = expression if " from " in expression else f"{expression} from a"
    assert _volatility(f"select {final}") == VolatilityResult(
        ok=False, reason_codes=(ReasonCode.NONDETERMINISTIC,), unknown_functions=(reported,)
    )


def test_volatility_reports_every_unknown_function_sorted():
    result = _volatility("select zeta(id), alpha(id), zeta(name) from a")
    assert result == VolatilityResult(
        ok=False, reason_codes=(ReasonCode.NONDETERMINISTIC,), unknown_functions=("alpha", "zeta")
    )


def test_deterministic_allowlist_never_names_a_volatile_or_order_sensitive_function():
    refused = {name.replace("_", "") for name in VOLATILE_FUNCTIONS | ORDER_SENSITIVE_FUNCTIONS}
    assert not {name.lower() for name in DETERMINISTIC_FUNCTIONS} & refused
