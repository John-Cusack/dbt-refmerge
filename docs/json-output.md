# JSON output

`scan --json` (or `--format json`), `check --json` and `fix --json` print one JSON document on stdout. `cleanup` always does. Everything else (progress, errors) goes to stderr.

The `scan` and `check` documents carry `"schema_version": "1"`. Within a schema version, fields are only ever added, never renamed or removed, and [reason codes](reason-codes.md) keep their names. Parse leniently: ignore fields you don't know.

Project-relative paths (`check` and `fix`) use forward slashes on every platform. Absolute paths (`project_dir`, and `scan`'s `source_path`) use the platform's separator.

## scan

```json
{
  "schema_version": "1",
  "command": "scan",
  "project_dir": "/work/analytics",
  "summary": {"findings": 1},
  "findings": [
    {
      "model_unique_id": "model.analytics.orders",
      "source_path": "/work/analytics/models/orders.sql",
      "upstream_unique_id": "model.analytics.stg_orders",
      "cte_names": ["orders", "order_financials"],
      "status": "needs_compiled_analysis",
      "reason_codes": ["NEEDS_COMPILED_ANALYSIS"],
      "line": 1
    }
  ]
}
```

| Field | Meaning |
|---|---|
| `model_unique_id` | The dbt unique id, when `target/manifest.json` names the file. Otherwise `model.<file stem>`. |
| `source_path` | Absolute path of the model file. |
| `upstream_unique_id` | The imported model or source, when the manifest resolves it. Otherwise `""`. |
| `cte_names` | Duplicate imports or imports that can be narrowed, as spelled in the source. Empty for unsupported shapes. |
| `status` | `needs_compiled_analysis` for every lead `scan` reports. |
| `reason_codes` | `NEEDS_COMPILED_ANALYSIS` for duplicate imports, `UNUSED_IMPORT_COLUMNS` for column pruning, or `UNSUPPORTED_IMPORT_SHAPE` when imports can't be merged as written. |
| `line` | 1-based line of the first duplicated import or projection that can be narrowed. |

## check

```json
{
  "schema_version": "1",
  "command": "check",
  "run_id": "20260916T120000_0123456789ab",
  "project_dir": "/work/analytics",
  "dbt": {
    "version": "1.10.4",
    "adapter_type": "postgres",
    "manifest_schema_version": "https://schemas.getdbt.com/dbt/manifest/v12.json"
  },
  "summary": {
    "models_scanned": 2,
    "findings": 1,
    "fixable": 1,
    "different": 0,
    "unverifiable": 0,
    "errors": 0
  },
  "models": [
    {
      "model_unique_id": "model.analytics.orders",
      "source_path": "models/orders.sql",
      "status": "snapshot_equivalent",
      "reason_codes": ["OK"],
      "warning_codes": [],
      "fixable": true,
      "equality": {
        "schema_equal": true,
        "baseline_rows": 1204,
        "candidate_rows": 1204,
        "baseline_only_occurrences": 0,
        "candidate_only_occurrences": 0
      },
      "diff": "--- a/models/orders.sql\n+++ b/models/orders.sql\n@@ ...",
      "scratch_relations": [
        {"database": "analytics", "schema": "refmerge_scratch", "identifier": "dbt_refmerge_baseline_000_0123456789ab_1a2b3c4d"},
        {"database": "analytics", "schema": "refmerge_scratch", "identifier": "dbt_refmerge_candidate_000_0123456789ab_1a2b3c4d"}
      ]
    }
  ],
  "workspace": null,
  "cleanup": {"complete": true, "objects": []}
}
```

| Field | Meaning |
|---|---|
| `run_id` | Identifies the run's scratch views; pass it to `cleanup --run-id`. |
| `dbt.version` | The dbt version reported by `dbt --version`. |
| `summary.models_scanned` | Models selected and compiled. |
| `summary.findings` | Models whose merge was proven equivalent. |
| `summary.fixable` | Models `fix` would apply (proven, and cleanup completed). |
| `summary.different` / `unverifiable` / `errors` | Models with that status. |
| `models[]` | One entry per selected model, sorted by `model_unique_id`. |
| `models[].source_path` | Relative to the project. |
| `models[].status` | `not_run`, `snapshot_equivalent`, `different`, `unverifiable` or `error`; see [statuses](verification.md#statuses). |
| `models[].fixable` | Whether `fix` would write this model. |
| `models[].equality` | Row counts from the comparison. All zero when the model didn't reach it. |
| `models[].diff` | Unified diff of the rewrite. Empty unless the model reached the warehouse. |
| `models[].scratch_relations` | The views created for this model. They are dropped by the end of the run. |
| `workspace` | Path of the kept workspace with `--keep-workspace`, else `null`. |
| `cleanup.complete` | `false` if any scratch view might remain. If so, run `cleanup --run-id`. |

## fix

```json
{
  "applied": true,
  "dry_run": false,
  "reason": "applied",
  "model_unique_id": "model.analytics.orders",
  "status": "snapshot_equivalent",
  "reason_codes": ["OK"],
  "diff": "--- a/models/orders.sql\n+++ b/models/orders.sql\n@@ ..."
}
```

`reason` is one of:
- `applied`;
- `dry-run`;
- `not fixable` (see `status` and `reason_codes`).

When the model isn't found, `fix` prints `fix: model not found` on stderr and exits 1 without a document.

## cleanup

```json
{
  "run_id": "20260916T120000_0123456789ab",
  "schema": "refmerge_scratch",
  "dropped": ["dbt_refmerge_baseline_000_0123456789ab_1a2b3c4d"],
  "remaining": [],
  "complete": true
}
```

`dropped` lists the views this call dropped, and `remaining` lists views of the run that still exist. `cleanup` exits 1 when `complete` is `false`.
