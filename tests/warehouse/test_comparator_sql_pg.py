"""The verdict SQL on real PostgreSQL: multiset semantics for duplicates, NULLs, empties and odd names."""

from collections import Counter

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from dbt_refmerge.verification.comparator import generate_except_all_sql, generate_grouped_counts_sql, quote_ident

pytestmark = pytest.mark.warehouse

STRATEGIES = [generate_except_all_sql, generate_grouped_counts_sql]


def _verdict(pg_dsn, schema, generate, columns, column_types, baseline_rows, candidate_rows):
    import psycopg2

    cols = ", ".join(f"{quote_ident(c)} {t}" for c, t in zip(columns, column_types, strict=True))
    placeholders = ", ".join(["%s"] * len(columns))
    with psycopg2.connect(pg_dsn) as conn, conn.cursor() as cur:
        cur.execute(f'create schema if not exists "{schema}"')
        for table, rows in (("baseline", baseline_rows), ("candidate", candidate_rows)):
            cur.execute(f'drop table if exists "{schema}"."{table}"')
            cur.execute(f'create table "{schema}"."{table}" ({cols})')
            for row in rows:
                cur.execute(f'insert into "{schema}"."{table}" values ({placeholders})', row)
        cur.execute(generate(f'"refmerge"."{schema}"."baseline"', f'"refmerge"."{schema}"."candidate"', list(columns)))
        return cur.fetchone()


CASES = {
    "same-rows-different-order": ([(1, "a"), (2, "b")], [(2, "b"), (1, "a")], (2, 2, 0, 0)),
    "duplicates-moved-between-values": ([(1, "a"), (1, "a"), (2, "b")], [(1, "a"), (2, "b"), (2, "b")], (3, 3, 1, 1)),
    "lost-duplicate": ([(1, "a"), (1, "a")], [(1, "a")], (2, 1, 1, 0)),
    "nulls-equal-as-values": ([(1, None), (None, None)], [(None, None), (1, None)], (2, 2, 0, 0)),
    "null-duplicate-lost": ([(None, None), (None, None)], [(None, None)], (2, 1, 1, 0)),
    "null-is-not-zero": ([(1, None)], [(1, "")], (1, 1, 1, 1)),
    "both-empty": ([], [], (0, 0, 0, 0)),
    "candidate-gains-rows": ([], [(1, "a"), (1, "a")], (0, 2, 0, 2)),
    "case-sensitive-text": ([(1, "A")], [(1, "a")], (1, 1, 1, 1)),
    "trailing-space-text": ([(1, "a")], [(1, "a ")], (1, 1, 1, 1)),
}


@pytest.mark.parametrize("generate", STRATEGIES, ids=["except_all", "grouped_counts"])
@pytest.mark.parametrize("case", sorted(CASES))
def test_verdict_counts(pg_dsn, scratch_schema, generate, case):
    baseline, candidate, expected = CASES[case]
    assert (
        _verdict(pg_dsn, scratch_schema, generate, ("id", "label"), ("integer", "text"), baseline, candidate)
        == expected
    )


@pytest.mark.parametrize("generate", STRATEGIES, ids=["except_all", "grouped_counts"])
def test_verdict_handles_hostile_column_names(pg_dsn, scratch_schema, generate):
    columns = ("Order ID", 'a"b', "__dbt_refmerge_side", "_a", "__dbt_refmerge_delta")
    types = ("integer",) * len(columns)
    rows = [(1, 2, 3, 4, 5), (1, 2, 3, 4, 5)]
    assert _verdict(pg_dsn, scratch_schema, generate, columns, types, rows, rows[:1]) == (2, 1, 1, 0)


@pytest.mark.parametrize("generate", STRATEGIES, ids=["except_all", "grouped_counts"])
def test_numeric_scale_compares_by_value(pg_dsn, scratch_schema, generate):
    # Documented behaviour: numeric equality is by value, so 1.0 and 1.00 are the same row.
    assert _verdict(pg_dsn, scratch_schema, generate, ("x",), ("numeric",), [("1.0",)], [("1.00",)]) == (1, 1, 0, 0)


rows_strategy = st.lists(
    st.tuples(st.one_of(st.none(), st.integers(-2, 2)), st.one_of(st.none(), st.sampled_from(["", "a", "b"]))),
    max_size=6,
)


@pytest.mark.parametrize("generate", STRATEGIES, ids=["except_all", "grouped_counts"])
@settings(max_examples=25, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(baseline=rows_strategy, candidate=rows_strategy)
def test_verdict_matches_multiset_arithmetic(pg_dsn, scratch_schema, generate, baseline, candidate):
    b, c = Counter(baseline), Counter(candidate)
    expected = (len(baseline), len(candidate), sum((b - c).values()), sum((c - b).values()))
    assert _verdict(pg_dsn, scratch_schema, generate, ("x", "y"), ("integer", "text"), baseline, candidate) == expected
