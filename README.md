# dbt-refmerge

[![CI](https://github.com/John-Cusack/dbt-refmerge/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/John-Cusack/dbt-refmerge/actions/workflows/ci.yml)
[![Coverage](https://codecov.io/gh/John-Cusack/dbt-refmerge/graph/badge.svg)](https://codecov.io/gh/John-Cusack/dbt-refmerge)
[![PyPI](https://img.shields.io/pypi/v/dbt-refmerge)](https://pypi.org/project/dbt-refmerge/)
[![Python](https://img.shields.io/pypi/pyversions/dbt-refmerge)](https://pypi.org/project/dbt-refmerge/)

Finds duplicated import CTEs in your dbt models — two CTEs reading different
columns from the same `{{ ref(...) }}` — proves the merged version returns
identical rows on your warehouse, and rewrites the file. Anything it can't
prove, it leaves alone.

## Use it

```sh
pip install dbt-refmerge
cd your-dbt-project
dbt-refmerge scan
```

That's it. The dialect is detected automatically from your
`profiles.yml`/`manifest.json` (override with `--adapter postgres` if
needed). You'll get one line per opportunity:

```
models/marts/orders.sql: order_items, order_items_summary -> ?
```

Each line is a *lead*: two CTEs over the same upstream model that may merge
into one. Then, on a branch:

```sh
git checkout -b refmerge-cleanup
export DBT_REFMERGE_SCRATCH_SCHEMA=refmerge_scratch     # where verification views go
dbt-refmerge check                                      # proves each merge on your warehouse
dbt-refmerge fix models/marts/orders.sql --dry-run      # preview the rewrite
dbt-refmerge fix models/marts/orders.sql                # re-proves, then applies it
git diff                                                # review, test, open a PR
```

`scan` and `check` never touch your files. `fix` refuses to write unless the
proof passes on current data (`applied=false` plus a reason means it's
working, not broken).

To prove a merge, `check` builds the original and the merged model as two
views in the scratch schema (dbt creates it if needed; it must not be a schema
your models build into), compares their column types and their rows as
multisets in a single query, then drops both views. If a run is interrupted,
`dbt-refmerge cleanup --run-id <id>` drops whatever it left (the id is in
`check --json`). Verification supports PostgreSQL in v0.1; `scan` parses 16
dialects (snowflake, bigquery, duckdb, databricks, redshift, trino, spark,
sqlite, tsql, oracle, exasol, clickhouse, and more).

## Develop it

Keep the inner test loop focused and in memory:

```sh
pip install -e ".[dev,integration]"
python3 -m pytest -q tests/unit/test_odd_scenarios.py  # odd-scenario safety lane
python3 -m pytest -q                                  # unit lane (default)
python3 -m ruff check .
python3 -m mypy --strict src
```

The default run is the in-memory unit lane and takes a few seconds on a
typical development machine. Two slower lanes are opt-in:

```sh
python3 -m pytest -q -m fake_dbt      # subprocess tests against tests/fakes/fake_dbt.py

docker run --rm -d --name refmerge-pg -p 5432:5432 \
  -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=refmerge postgres:16
export REFMERGE_TEST_PG_DSN=postgresql://postgres:postgres@localhost:5432/refmerge
python3 -m pytest -q -m warehouse     # needs Postgres and dbt-postgres
python3 -m pytest -q -m "" --cov      # every lane; enforces the coverage floor
```

`TEST_COVERAGE_PLAN.md` tracks the path to 100% coverage.

## Support

If this saves you an afternoon, [buy me a coffee](https://buymeacoffee.com/johncusack).

MIT license. Requires Python ≥ 3.11 and your own dbt installation.
