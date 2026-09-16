"""Semantics: parsing, import allowlist, fingerprints, volatility, expected transform."""

import pytest

from dbt_refmerge.domain import ReasonCode
from dbt_refmerge.errors import SemanticError
from dbt_refmerge.semantics import (
    DETERMINISTIC_FUNCTIONS,
    ORDER_SENSITIVE_FUNCTIONS,
    VOLATILE_FUNCTIONS,
    Qualification,
    VolatilityResult,
    analyze_volatility,
    build_expected_transform,
    parse_model,
    predicate_fingerprint,
    qualify_import_cte,
)


@pytest.mark.parametrize(
    ("sql", "reason", "message"),
    [
        ("  \n", ReasonCode.INTERNAL_ERROR, "empty compiled SQL"),
        ("select {{ x }}", ReasonCode.HARNESS_EMBEDDING_UNSAFE, "unresolved Jinja in compiled SQL"),
        ("select 1 {% endraw %}", ReasonCode.HARNESS_EMBEDDING_UNSAFE, "unresolved Jinja in compiled SQL"),
        ("select '%}'", ReasonCode.HARNESS_EMBEDDING_UNSAFE, "unresolved Jinja in compiled SQL"),
        ("select (", ReasonCode.INTERNAL_ERROR, "compiled parse failed: "),
        ("select 1; select 2", ReasonCode.INTERNAL_ERROR, "compiled SQL must be one statement"),
        (";", ReasonCode.INTERNAL_ERROR, "compiled SQL must be one statement"),
        ("; -- c", ReasonCode.INTERNAL_ERROR, "unexpected semicolon node"),
        ("insert into t values (1)", ReasonCode.INTERNAL_ERROR, "non-embeddable compiled root: Insert"),
    ],
    ids=[
        "blank",
        "jinja-expression",
        "jinja-statement",
        "jinja-closer-in-string",
        "unparseable",
        "two-statements",
        "bare-semicolon",
        "semicolon-with-comment",
        "insert",
    ],
)
def test_parse_model_refuses_unembeddable_compiled_sql(sql, reason, message):
    with pytest.raises(SemanticError) as exc_info:
        parse_model(sql)
    assert exc_info.value.reason_code is reason
    assert exc_info.value.message.startswith(message)


def test_parse_model_without_with_has_no_ctes():
    parsed = parse_model("select 1\n")
    assert (parsed.sql, parsed.dialect, parsed.ctes) == ("select 1", "postgres", {})


def test_parse_model_skips_cte_with_empty_quoted_name():
    parsed = parse_model('with "" as (select 1), a as (select 2) select * from a')
    assert list(parsed.ctes) == ["a"]


def test_direct_import_qualifies():
    p = parse_model("with a as (select x, y from sch.tbl) select * from a", "postgres")
    assert qualify_import_cte(p.ctes["a"]) == Qualification(ok=True, reason_codes=(ReasonCode.OK,))


def test_join_rejected():
    p = parse_model("with a as (select x from t1 join t2 on true) select * from a", "postgres")
    assert not qualify_import_cte(p.ctes["a"]).ok


@pytest.mark.parametrize(
    ("body", "unexpected"),
    [
        ("select id from db.sch.stg union all select id from db.sch.stg", ("Union",)),
        ("select id from db.sch.stg join db.sch.other using (id)", ("Join",)),
        ("select id from db.sch.stg group by id", ("group",)),
        ("select id from db.sch.stg order by id", ("order",)),
        ("select id from db.sch.stg limit 1", ("limit",)),
        ("select distinct id from db.sch.stg", ("Distinct",)),
        ("select * from db.sch.stg", ("Star",)),
        ("select lower(id) as id from db.sch.stg", ("Lower",)),
        ("select id + 1 as id from db.sch.stg", ("Add",)),
        ("select id from db.sch.stg where id in (select id from db.sch.other)", ("Subquery",)),
        ("select id", ("MissingFrom",)),
    ],
    ids=[
        "union",
        "join",
        "group-by",
        "order-by",
        "limit",
        "distinct",
        "star",
        "function",
        "arithmetic",
        "subquery",
        "no-from",
    ],
)
def test_qualify_import_cte_refuses_unsupported_shapes(body, unexpected):
    parsed = parse_model(f"with a as ({body}) select * from a")
    assert qualify_import_cte(parsed.ctes["a"]) == Qualification(
        ok=False, reason_codes=(ReasonCode.UNSUPPORTED_IMPORT_SHAPE,), unexpected_nodes=unexpected
    )


def test_predicate_fingerprint_non_select_is_none():
    parsed = parse_model("with a as (select id from t where id > 1 union all select id from u) select * from a")
    assert predicate_fingerprint(parsed.ctes["a"]) is None


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
        "localtimestamp",
        "localtime",
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


_TWO_IMPORTS = "with a as (select id from t), b as (select id from t) select * from b"


@pytest.mark.parametrize(
    ("baseline", "canonical", "additions", "message"),
    [
        ("select 1", "a", [], "baseline has no WITH"),
        (_TWO_IMPORTS, "missing", [], "canonical CTE missing in baseline"),
        (
            "with a as (select id from t union all select id from u), b as (select id from t) select * from b",
            "a",
            [],
            "canonical not a select",
        ),
        (_TWO_IMPORTS, "a", ["from"], "bad projection from: "),
    ],
    ids=["no-with", "unknown-canonical", "union-canonical", "unparseable-addition"],
)
def test_expected_transform_refuses_inconsistent_baseline(baseline, canonical, additions, message):
    with pytest.raises(SemanticError) as exc_info:
        build_expected_transform(parse_model(baseline), canonical, ("b",), {canonical: additions})
    assert exc_info.value.reason_code is ReasonCode.COMPILE_DRIFT
    assert exc_info.value.message.startswith(message)


@pytest.mark.parametrize(
    "final",
    [
        "select id, amount from a order by id limit 5",
        "select * from a order by id limit 5",
        "select a.* from a order by id limit 5",
        "select distinct on (id) id, amount from a order by id",
        "select id, amount from a order by id offset 5",
        "select id, amount from a union all select id, amount from a order by 1 limit 3",
    ],
    ids=["one-of-two-columns", "star", "qualified-star", "distinct-on-partial", "offset-partial", "union-partial"],
)
def test_volatility_refuses_row_selection_whose_order_can_tie(final):
    # Rows that tie on the ORDER BY but differ in another output column make LIMIT pick different rows.
    result = analyze_volatility(parse_model(f"with a as (select id, amount from t) {final}", "postgres"))
    assert (result.ok, result.reason_codes) == (False, (ReasonCode.NONDETERMINISTIC,))


@pytest.mark.parametrize(
    "final",
    [
        "select id, amount from a order by 1, 2 limit 5",
        "select id, amount as amt from a order by amt desc, id limit 5",
        "select id, sum(amount) as total from a group by id order by id, sum(amount) limit 5",
        "select a.id, a.amount from a order by a.amount, a.id limit 5",
        "select id, amount from a order by amount, id nulls first limit 5",
        "select distinct on (id) id, amount from a order by id, amount",
        "select id, amount from a union all select id, amount from a order by 2, 1 limit 3",
        "select id, amount + 1 from a order by amount + 1, id limit 5",
    ],
    ids=[
        "ordinals",
        "alias",
        "expression",
        "qualified",
        "nulls-first",
        "distinct-on-total",
        "union-total",
        "unaliased-expression",
    ],
)
def test_volatility_accepts_row_selection_ordered_by_every_output(final):
    assert analyze_volatility(parse_model(f"with a as (select id, amount from t) {final}", "postgres")).ok
