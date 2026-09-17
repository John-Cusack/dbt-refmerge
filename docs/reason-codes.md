# Reason codes

Every model result and every error carries a stable reason code. The one exception is `fix` for a path that isn't a model in the project: it prints `fix: model not found` and exits 1. Codes appear in three places:
- `reason_codes` in `check --json` and `fix --json`;
- `reason_codes` in `scan --json` findings;
- the prefix of error messages, such as `check failed: [UNSUPPORTED_ADAPTER] …`, which exit 1.

In the headings below:
- **Status** is the `check` status a model gets with this code.
- **Error** means the whole command stops with exit code 1.

Codes are never renamed within a JSON `schema_version`.

## Results

### OK
**Status:** `snapshot_equivalent`. The rewrite was proven. `fix` can apply it, provided cleanup completed.

### NO_DUPLICATE_IMPORT
**Status:** `not_run`. No supported duplicate merge or import column pruning was found. The historical code name is retained for compatibility. `--fail-on` never counts it.

### UNUSED_IMPORT_COLUMNS
**Seen in:** `scan` findings. A direct import can select only the columns its consumers need, replacing a wildcard or removing unused explicit columns.
**What to do:** on PostgreSQL, run `check` to verify the [column pruning](column-pruning.md), then `fix` to apply it. Other dialects support source-only `scan`.

### NEEDS_COMPILED_ANALYSIS
**Seen in:** `scan` findings. The source has duplicate imports, but whether they can be merged depends on the compiled SQL.
**What to do:** run `check` on the model.

### SCHEMA_MISMATCH
**Status:** `different`. The merged model's columns differ from the original's in name, order or exact type.
**What to do:** nothing is applied. Report the model as a bug if the rewrite looked correct to you.

### BAG_DIFFERENCE
**Status:** `different`. The merged model returns different rows, or the same rows a different number of times. `baseline_only_occurrences` and `candidate_only_occurrences` count the differing rows.
**What to do:** nothing is applied. Please report it, with the diff, as a bug.

## Refused by static analysis

These refusals happen before anything touches the warehouse. None of them is a malfunction: the model stays as it is.

### UNSUPPORTED_MODEL_TYPE
**Status:** `unverifiable`. The model may import a relation twice, but it is materialized as something other than `table` or `view` (for example `incremental` or `ephemeral`), which dbt-refmerge doesn't rewrite.

### UNSUPPORTED_IMPORT_SHAPE
**Status:** `unverifiable`; also seen in `scan` findings. An import CTE isn't a plain `select col, col as alias from {{ ref(...) }} [where ...]`. Common causes:
- `distinct`, `group by`, joins or expressions in the import;
- `select *` expanded downstream;
- unsupported projection formatting, such as CR-only newlines;
- `WITH RECURSIVE`, or CTE column lists;
- a name reserved for internal sentinels (`__r0__`).

**What to do:** merge by hand, or reshape the imports into plain column lists and run `check` again.

### DIFFERENT_PREDICATE
**Status:** `unverifiable`. The imports filter the relation differently, so merging them would change results.

### PROJECTION_COLLISION
**Status:** `unverifiable`. The merged column list would give one output name to two different upstream columns (`a as x` in one import, `b as x` in another), or one import repeats an output name.

### SOURCE_MAPPING_AMBIGUOUS
**Status:** `unverifiable`. dbt-refmerge couldn't tie the source to what dbt compiled. Causes:
- the file at the manifest's `original_file_path` isn't in the project;
- a source CTE has no matching compiled CTE, or a different number of columns;
- the imports resolve to different compiled relations;
- a `ref()`/`source()` resolves ambiguously in the manifest.

### REFERENCE_BINDING_AMBIGUOUS
**Status:** `unverifiable`. After the merge, a later reference might bind to a different column or CTE. Causes:
- an unqualified column in a query that joins several relations would now match a column gained from another import;
- a nested CTE shadows an import's name;
- a macro or expression refers to an import in a way that can't be resolved.

**What to do:** qualify the columns (`orders.order_id`) and run `check` again.

### COMMENT_RELOCATION_UNSUPPORTED
**Status:** `unverifiable`. A SQL comment or non-`ref` Jinja sits inside the text the rewrite would delete or extend, and moving it could change its meaning.
**What to do:** move or remove the comment and run `check` again.

### NONDETERMINISTIC
**Status:** `unverifiable`. Something in the model could return different rows from one run to the next, regardless of the merge, so a comparison would prove nothing. Causes:
- a volatile function such as `random()`, `now()` or `nextval()`;
- any function not on dbt-refmerge's list of known deterministic functions, including user-defined functions;
- `TABLESAMPLE`;
- a `ROWS` window frame;
- `LIMIT`, `OFFSET` or `DISTINCT ON` without an `ORDER BY` covering every output column.

### COMPILE_DRIFT
**Status:** `unverifiable`. The rewritten model's compiled SQL didn't change exactly as the merge predicts, or dbt didn't compile it. A macro may render differently for the rewritten source.

### HARNESS_EMBEDDING_UNSAFE
**Status:** `unverifiable`. The compiled SQL can't be embedded safely in the verification views. Causes:
- unrendered Jinja (`{{`, `{%`);
- several statements;
- a backslash or NUL in a name;
- a profile name with unusual characters.

## Environment and configuration

### UNSUPPORTED_MANIFEST_SCHEMA
**Error**, or `unverifiable` for a rewritten model. dbt wrote a manifest schema other than v10–v12.
**What to do:** use dbt 1.6 or later.

### UNSUPPORTED_ADAPTER
**Error.** One of:
- the adapter name is unknown;
- the adapter couldn't be determined;
- `check` was run with an adapter it can't verify (only `postgres`).

**What to do:** pass `--adapter`, or set `adapter` in `.dbt-refmerge.toml`. See [adapters](configuration.md#adapters).

### ADAPTER_MISMATCH
**Error.** `--adapter`, the compiled manifest and `profiles.yml` disagree about the warehouse. Often a stale `target/manifest.json` is the cause.
**What to do:** recompile for the right target, or pass `--adapter` explicitly.

### SCRATCH_BOUNDARY_VIOLATION
**Error**, or `unverifiable` for a model. One of:
- no scratch schema is set;
- the scratch schema is forbidden, too long, or shared with a model's schema;
- the harness manifest contained something other than the expected views in the scratch schema;
- two models' scratch view names would collide.

**What to do:** set `--scratch-schema` to a dedicated schema.

### SOURCE_CHANGED_DURING_SNAPSHOT
**Error.** A project file changed while `check` was copying it.
**What to do:** run again.

### SOURCE_CHANGED_BEFORE_APPLY
**Error** (`fix`). The model file changed between the proof and the write, or is no longer a regular file. Nothing was written.
**What to do:** run `fix` again.

## dbt and the warehouse

### UNSUPPORTED_COMPARISON_TYPE
**Status:** `unverifiable`. A column has a type dbt-refmerge doesn't compare exactly, such as `double precision`, `real`, `json`, `jsonb`, an array, or an interval. The [type list](verification.md#limits) has the full set of supported types.
**What to do:** cast the column to a supported type in the model, or merge by hand.

### DBT_COMMAND_FAILED
**Status:** depends on what failed:
- **`error`:** dbt failed while proving the model, because a verification view couldn't be built or a comparison query failed;
- **`unverifiable`:** a rewritten model didn't compile, or a query returned something malformed;
- **error (exit 1):** dbt failed on the whole project, e.g. `dbt --version` or the baseline compile.

Query timeouts (`warehouse_statement_timeout_ms`) show up here too.
**What to do:** rerun with `--debug` and `--keep-workspace` to see dbt's output and artifacts.

### CLEANUP_FAILED
**Error** (`cleanup`). The run id isn't of the form `20260916T120000_0123456789ab`.

### INTERNAL_ERROR
**Error**, or `unverifiable` for a model. One of:
- a manifest dbt-refmerge can't read (too large, invalid JSON, duplicate keys);
- a model file that isn't UTF-8 or has unterminated Jinja;
- a symbolic link or special file in the project that can't be snapshotted safely.

**What to do:** if none of these apply, please [open an issue](https://github.com/John-Cusack/dbt-refmerge/issues/new/choose).

## Reserved

These codes are defined so JSON consumers can rely on them, but this version doesn't emit them.

### COMPILE_REQUIRES_INTROSPECTION
Reserved for models whose compilation needs warehouse introspection.

### INPUT_ISOLATION_UNAVAILABLE
Reserved for warehouses that can't compare both sides within one snapshot.

### WAREHOUSE_TIMEOUT
Reserved; timeouts are currently reported as `DBT_COMMAND_FAILED`.

### RESOURCE_LIMIT
Reserved for query cost or size limits.
