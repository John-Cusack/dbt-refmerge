# Verification

`check` and `fix` never rely on the rewrite being correct. They prove that the merged model returns the same rows as the original on your warehouse, and they refuse whenever they can't.

## What a `check` does

1. **Snapshot.** Copies the project into a private temporary directory (the workspace), so edits made during the run can't mix into the proof. Symbolic links that leave the project are refused. `dbt_packages` links created by `dbt deps` are copied.
2. **Baseline compile.** Runs `dbt compile` for the selected models, writing artifacts inside the workspace. It never reads a stale `target/`. dbt manifest schemas v10 to v12 are supported.
3. **Static analysis and rewrite**, per model. A model is merged only when it:
   - is materialized as a `table` or `view`;
   - parses;
   - imports the same `ref()`/`source()` in several CTEs, with plain column lists and the same predicate;
   - has no nondeterministic functions.

   dbt-refmerge also checks how later CTEs refer to the imports. The rewrite goes to a copy of the model inside the workspace.

   Models that can't be analyzed are refused only when two literal `ref()`/`source()` calls in them may name the same relation. Otherwise they have nothing to merge (`NO_DUPLICATE_IMPORT`).
4. **Candidate compile and delta gate.** Compiles every rewritten model in one `dbt compile`. The compiled SQL must differ from the baseline exactly as the merge predicts; anything else is `COMPILE_DRIFT`.
5. **Warehouse proof**, for all remaining models together:
   - writes a throwaway dbt project (the harness) holding, for each model, the baseline and candidate compiled SQL as two views;
   - runs `dbt parse` and checks the harness manifest before anything is created. It must contain exactly those views, with no hooks, all in the scratch schema;
   - runs `dbt run` to create the views;
   - reads every view's column names and exact types from `pg_catalog`;
   - compares each pair's rows as multisets (`EXCEPT ALL` in both directions, plus row counts), each comparison in one SQL statement so both sides see the same snapshot;
   - drops the views and confirms they are gone.
6. **Report.** Each model gets a status, reason codes, row counts and a diff.

The number of dbt invocations doesn't grow with the number of models. Failures stay per model:
- a candidate that breaks compilation is retried alone;
- a view dbt can't build fails only its model;
- a comparison that errors is retried alone.

## Statuses

| Status | Meaning | Fixable |
|---|---|---|
| `not_run` | No duplicate imports to merge (`NO_DUPLICATE_IMPORT`) | no |
| `snapshot_equivalent` | Same column names and types in the same order, and the same rows with the same multiplicities | yes, if the views were dropped |
| `different` | The merge would change the model's output (`SCHEMA_MISMATCH` or `BAG_DIFFERENCE`) | no |
| `unverifiable` | dbt-refmerge refused to prove it; the [reason code](reason-codes.md) says why | no |
| `error` | A dbt command failed while proving it | no |

`fix` writes a file only for `snapshot_equivalent` with reason `OK` and complete cleanup.

## What "snapshot equivalent" does and doesn't mean

The proof covers your data as it is at the moment of the comparison. A merge that is equivalent today could, in principle, differ on data that doesn't exist yet. For example, a predicate might filter rows that only appear later.

The static gates exist to rule out the realistic cases. The merge is only attempted when every import selects plain columns from the same relation with the same predicate, and the compiled SQL changes exactly as expected. The warehouse comparison then catches anything the static analysis missed.

Treat a `snapshot_equivalent` result as strong evidence plus a reviewable diff, not a formal proof.

## The scratch schema

`scratch_schema` names the schema that receives the views.

**Requirements:**
- It must not be a schema any model builds into, since sharing one is refused.
- It may not be `pg_catalog` or `information_schema`.
- Its name may be at most 63 bytes.
- Quoted names (`"Scratch"`) keep their case; unquoted names fold to lower case.

**Behavior:**
- dbt creates the schema if it doesn't exist; dbt-refmerge never drops it.
- Every view is named `dbt_refmerge_baseline_<run>_<model hash>` or `dbt_refmerge_candidate_<run>_<model hash>`. Names are unique per run and model.
- Views are always dropped at the end, even when verification fails. A dropped view is confirmed gone through the catalog. If a drop fails, the model isn't fixable and `check --json` reports `cleanup.complete: false`.
- `dbt-refmerge cleanup --run-id <id>` drops leftovers from one run. It only drops views matching that run's names.

## Privileges

`check` connects through your dbt profile, so the profile's database user needs:

- `CREATE` on the database, if the scratch schema doesn't exist yet, or `CREATE` and `USAGE` on the scratch schema;
- `SELECT` (and `USAGE` on their schemas) on every relation the checked models read;
- permission to read `pg_catalog`, which PostgreSQL grants by default.

It never writes to your models' schemas and never creates tables.

## Limits

- **Warehouses:** PostgreSQL only. Other adapters can use `scan`.
- **Column types:** allowlisted, compared exactly: `boolean`, `smallint`, `integer`, `bigint`, `numeric`, `text`, `character varying`/`varchar`, `character`/`char`, `date`, `timestamp` with and without time zone, `uuid` and `bytea`. Any other type, such as `double precision`, `json` or arrays, is `UNSUPPORTED_COMPARISON_TYPE`.
- **Time limits:** each query runs under `statement_timeout = warehouse_statement_timeout_ms` (default 15 minutes), and each dbt command under `subprocess_timeout_seconds` (default 30 minutes).
- **Cost:** the comparison reads both sides in full. On large models, point `--target` at a smaller development database or use `--select` to check fewer models.
- **Materializations:** `incremental`, `ephemeral`, snapshots and other materializations aren't merged.
