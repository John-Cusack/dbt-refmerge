# dbt-refmerge

[![CI](https://github.com/John-Cusack/dbt-refmerge/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/John-Cusack/dbt-refmerge/actions/workflows/ci.yml)
[![Coverage](https://codecov.io/gh/John-Cusack/dbt-refmerge/graph/badge.svg)](https://codecov.io/gh/John-Cusack/dbt-refmerge)
[![PyPI](https://img.shields.io/pypi/v/dbt-refmerge)](https://pypi.org/project/dbt-refmerge/)
[![Python](https://img.shields.io/pypi/pyversions/dbt-refmerge)](https://pypi.org/project/dbt-refmerge/)
[![OpenSSF Scorecard](https://api.scorecard.dev/projects/github.com/John-Cusack/dbt-refmerge/badge)](https://scorecard.dev/viewer/?uri=github.com/John-Cusack/dbt-refmerge)

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
models/marts/orders.sql:3: CTEs order_items, order_items_summary import model.shop.stg_order_items; run dbt-refmerge check to prove a merge
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
multisets in a single query, then drops both views. All models in a `check`
share one batch of dbt calls, so a large project costs about as many dbt
invocations as a small one, and a model that fails doesn't block the others.
If a run is interrupted, `dbt-refmerge cleanup --run-id <id>` drops whatever
it left (the id is in `check --json`). Verification supports PostgreSQL;
`scan` parses 16 dialects (snowflake, bigquery, duckdb, databricks, redshift,
trino, spark, sqlite, tsql, oracle, exasol, clickhouse, and more).

## Keep new duplicates out

`scan` needs no dbt run and no warehouse, so it works as a commit hook or a
pull request check. The hook and the action ship with 0.2.0; until that
release, pin `rev:` and `uses:` to a commit on `main`.

```yaml
# .pre-commit-config.yaml
- repo: https://github.com/John-Cusack/dbt-refmerge
  rev: v0.2.0
  hooks:
    - id: dbt-refmerge-scan
```

```yaml
# a GitHub Actions step: annotates pull requests at each duplicate
- uses: John-Cusack/dbt-refmerge@v0.2.0
  with:
    adapter: snowflake
```

See [integrations](https://github.com/John-Cusack/dbt-refmerge/blob/main/docs/integrations.md) for options and other CI systems.

## Documentation

- [Command-line reference](https://github.com/John-Cusack/dbt-refmerge/blob/main/docs/cli.md), with exit codes
- [Configuration](https://github.com/John-Cusack/dbt-refmerge/blob/main/docs/configuration.md): `.dbt-refmerge.toml`, environment variables and adapters
- [Verification](https://github.com/John-Cusack/dbt-refmerge/blob/main/docs/verification.md): what `check` creates, the privileges it needs and its limits
- [Reason codes](https://github.com/John-Cusack/dbt-refmerge/blob/main/docs/reason-codes.md): why a model was refused and what to do about it
- [JSON output](https://github.com/John-Cusack/dbt-refmerge/blob/main/docs/json-output.md)
- [Integrations](https://github.com/John-Cusack/dbt-refmerge/blob/main/docs/integrations.md): pre-commit, GitHub Actions and other CI

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

See [CONTRIBUTING.md](https://github.com/John-Cusack/dbt-refmerge/blob/main/CONTRIBUTING.md) for conventions and the release process, and
[SECURITY.md](https://github.com/John-Cusack/dbt-refmerge/blob/main/SECURITY.md) to report a vulnerability. [IMPROVEMENT_PLAN.md](https://github.com/John-Cusack/dbt-refmerge/blob/main/IMPROVEMENT_PLAN.md) tracks what comes next.

## Support

If this saves you an afternoon, [buy me a coffee](https://buymeacoffee.com/johncusack).

MIT license. Requires Python ≥ 3.11 and your own dbt installation.
