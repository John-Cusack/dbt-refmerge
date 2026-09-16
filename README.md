# dbt-refmerge

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
dbt-refmerge check --scratch-schema refmerge_scratch   # proves each merge on your warehouse
dbt-refmerge fix models/marts/orders.sql --dry-run     # preview the rewrite
dbt-refmerge fix models/marts/orders.sql               # apply it, one model at a time
git diff                                                # review, test, open a PR
dbt-refmerge cleanup --run-id <id>                      # drops scratch tables (id is in check --json)
```

`scan` and `check` never touch your files. `fix` refuses to write unless the
proof passes on current data (`applied=false` plus a reason means it's
working, not broken). Verification supports PostgreSQL in v0.1; `scan` parses
16 dialects (snowflake, bigquery, duckdb, databricks, redshift, trino, spark,
sqlite, tsql, oracle, exasol, clickhouse, and more).

## Support

If this saves you an afternoon, [buy me a coffee](https://buymeacoffee.com/johncusack).

MIT license. Requires Python ≥ 3.11 and your own dbt installation.
