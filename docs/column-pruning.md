# Selecting only needed columns

`scan`, `check` and `fix` automatically look for direct `ref()` and `source()` import CTEs that can select fewer columns. This works for a single import as well as duplicate imports. No extra flag or upstream model schema is needed.

For example:

```sql
with orders as (
    select * from {{ ref('stg_orders') }}
)
select order_id, amount from orders where status = 'paid'
```

becomes:

```sql
with orders as (
    select order_id, amount, status from {{ ref('stg_orders') }}
)
select order_id, amount from orders where status = 'paid'
```

The analysis retains inputs used in projections, calculations, join conditions, `USING` keys, filters, grouping, `HAVING` and ordering. It combines the requirements of every consumer of an import. It also traces requirements through CTEs that forward a single source's `SELECT *`. An import's own `WHERE` remains intact; columns used only by that predicate need not be included in the import's output.

Unused plain columns in explicit import lists can also be removed. Column aliases and quoted names are retained. Narrowed duplicate imports are merged in the same rewrite when the existing merge gates permit it. Imports with different predicates can be narrowed separately.

`scan` reports `UNUSED_IMPORT_COLUMNS` as a lead. `check` recompiles the candidate, checks that the compiled SQL changed only as planned, and compares output column names, types, order and row multiplicities on PostgreSQL. `fix` writes only a verified candidate. Existing verification and materialization limits still apply; see [verification](verification.md).

## Snowflake and BigQuery

Column demand analysis runs on local SQL text. It does not query a warehouse or need a dbt adapter,
profile, credentials or an upstream schema. Use `dbt-refmerge scan --adapter snowflake` or
`dbt-refmerge scan --adapter bigquery` in your dbt project. Connections are needed only for the separate
warehouse verification step; `check` and `fix` remain available on PostgreSQL only.

For these dialects, the parser additionally supports:

- BigQuery backtick-quoted CTEs, columns and aliases. Column and query alias binding is case-insensitive,
  including quoted references; original quotation and spelling are preserved.
- Import wildcards using BigQuery `* EXCEPT (...)` or Snowflake `* EXCLUDE (...)`. Required columns must
  not be excluded. If an intermediate CTE excludes a column, that column remains in its upstream input
  so the intermediate exclusion still refers to an existing column.
- Qualified BigQuery nested fields such as `o.payload.customer.id`, retaining the entire `payload`
  column, and Snowflake VARIANT paths such as `o.payload:customer.id`.
- BigQuery `UNNEST` with a named element alias and one input array, and Snowflake lateral `FLATTEN`.
  Input arrays/payloads and references in consumers must be qualified when there are multiple sources.
  The row expansion itself remains intact.
- Window inputs used directly in `QUALIFY`, retaining their partition and ordering columns.

The final query still needs explicit output columns. Wildcard `REPLACE`, `RENAME` and `ILIKE`,
unqualified nested fields, implicit array expansion, `UNNEST ... WITH OFFSET`, unknown table functions,
`PIVOT`/`UNPIVOT`, BigQuery backtick tokens containing dotted paths or escape sequences, Snowflake output
alias reuse in other projections or `WHERE`, and `QUALIFY` references to output aliases are skipped.
Dialect fixtures test both successful narrowing and these limits
without warehouse connections. This is source-analysis coverage, not warehouse verification.

## Conservative limits

Pruning leaves the model unchanged when the final query projects `*` or `alias.*`. `COUNT(*)` does not require every column, but an import with no named column requirements is left alone.

The implementation skips nested queries, set operations, SQL-producing Jinja macros and control flow, whole-row references, table functions other than the supported `UNNEST`/`FLATTEN` forms above, ambiguous unqualified columns in joins, and natural joins. A wildcard cannot be narrowed when it has `DISTINCT`, grouping, multiple sources, additional projections, wildcard modifiers other than the supported `EXCEPT`/`EXCLUDE` forms above, select modifiers such as `TOP` or `SELECT AS STRUCT`, or ordering by a column position. Clause references to output aliases are supported for top-level `ORDER BY`; other alias-binding cases are skipped. SQL or Jinja comments between an import's `SELECT` and `FROM` prevent pruning that import, including comments before or after its projection list.

Explicit intermediate CTE projections are retained, so their input columns remain required even if a later query does not use every intermediate output. This can retain more columns than a full schema-aware optimizer would.

## Performance expectations

Selecting fewer columns can reduce scanning, I/O and intermediate materialization. [BigQuery recommends controlling projection](https://docs.cloud.google.com/bigquery/docs/best-practices-performance-compute#avoid_select_) for these reasons.

An internal `SELECT *` does not always cause extra work: databases can optimize across CTEs. [PostgreSQL documents folding eligible CTEs into the parent query](https://www.postgresql.org/docs/current/queries-with.html#QUERIES-WITH-CTE-MATERIALIZATION), while multiply referenced CTEs are normally materialized. The inference is that explicit narrowing may help when the planner retains wide intermediate results, and may have little effect when it already removes unused columns. A speedup is not guaranteed.

To measure the effect, compare execution plans, bytes scanned, intermediate widths and timings of the original and candidate SQL on representative data. `check` verifies equality; it does not benchmark performance. Only PostgreSQL currently supports `check`/`fix`; other configured dialects can use `scan`.

A local PostgreSQL 16 synthetic test used 100,000 rows, eight 128-character text columns, a 4 MB `work_mem`, and two consumers of one import CTE. Across five executions after warming both queries, median server execution time fell from 171 ms with `SELECT *` to 58 ms with `SELECT id` (about 2.9 times faster). The CTE's planned row width fell from 1,060 to 4 bytes, and temporary blocks written fell from 13,062 to 171. Both queries returned the same nine rows. These measurements describe this test, not an expected production speedup. [The example SQL](examples/column-pruning-benchmark.sql) includes both the reused CTE and a single-consumer case for comparing plans.
