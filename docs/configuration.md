# Configuration

Settings come from four places. The first one that sets a value wins:

1. **Command-line flags.** A flag you don't pass never overrides the sources below.
2. **Environment variables** named `DBT_REFMERGE_<KEY>`, e.g. `DBT_REFMERGE_SCRATCH_SCHEMA`.
3. **`.dbt-refmerge.toml`** in the project directory. Keys go either at the top level or under a `[tool.dbt-refmerge]` table. When that table is present, top-level keys are ignored.
4. **Defaults.**

The project directory comes only from `--project-dir`, or the working directory by default. A file or variable can't redirect it.

```toml
# .dbt-refmerge.toml
adapter = "postgres"
scratch_schema = "refmerge_scratch"
fail_on = "different"
warehouse_statement_timeout_ms = 300000
```

## Keys

| Key | Flag | Default | Meaning |
|---|---|---|---|
| `profiles_dir` | `--profiles-dir` | dbt's lookup | Directory holding `profiles.yml`. |
| `profile` | `--profile` | `profile:` in `dbt_project.yml` | dbt profile name. |
| `target` | `--target` | the profile's `target:` | dbt target name. |
| `dbt_command` | `--dbt-command-part` (repeated) | `dbt` | The dbt executable and any leading arguments. In TOML or the environment, a string is split like a POSIX shell would (`"uv run dbt"`), so quote paths with spaces. A TOML array is taken as is. |
| `adapter` | `--adapter` | detected | Warehouse adapter; see [adapters](#adapters). |
| `scratch_schema` | `--scratch-schema` | none | Schema for verification views. Required by `check`, `fix` and `cleanup`. See [verification](verification.md#the-scratch-schema). |
| `subprocess_timeout_seconds` | none | `1800` | Time limit for each dbt command. |
| `warehouse_statement_timeout_ms` | none | `900000` | PostgreSQL `statement_timeout` for every verification query. |
| `fail_on` | `--fail-on` | `fixable` | `never`, `finding`, `fixable`, `different` or `unverifiable`; see [exit codes](cli.md#exit-codes). |
| `keep_workspace` | `--keep-workspace` | `false` | Keep the private working copy after `check` or `fix`. |
| `json_output` | `--json` | `false` | JSON output; see [JSON output](json-output.md). |
| `debug` | `--debug` | `false` | Print tracebacks with errors. |

**Value rules:**
- **Booleans** in the environment are true when the value is `1`, `true` or `yes`, in any case.
- **Timeouts** must be positive, and at most 86,400,000.
- **Names** (`profile`, `target`, `scratch_schema`, `adapter`) may not contain control characters other than tab.
- **Unknown keys** are ignored, so check the spelling of a setting that seems to have no effect.

## dbt settings dbt-refmerge honors

- **`DBT_PROFILES_DIR`:** used when no `profiles_dir` is set. A relative value is resolved against the project directory, because dbt-refmerge runs dbt from a copy of the project.
- **The profile's own environment:** `env_var()` calls in `profiles.yml` or the project are resolved by dbt, from the environment dbt-refmerge was started in.

## Adapters

The adapter sets the SQL dialect used to parse models and how unquoted identifiers are compared. Only `postgres` can be verified (`check`, `fix`, `cleanup`). Every other name below works with `scan` only.

| Adapter | Unquoted identifiers | `check` |
|---|---|---|
| `postgres` (also `postgresql`, `pg`) | fold to lower case | yes |
| `redshift`, `materialize`, `duckdb`, `trino`, `presto`, `athena` | fold to lower case | no |
| `bigquery`, `databricks`, `spark`, `sqlite`, `tsql` | compared case-insensitively | no |
| `snowflake`, `oracle`, `exasol` | fold to upper case | no |
| `clickhouse` | exact spelling | no |

**Resolution order:**
1. `--adapter` (or `adapter` in configuration).
2. The compiled manifest's `adapter_type`: `scan` reads `target/manifest.json` when present, and `check` reads the manifest it compiles.
3. The `type:` of the selected target in `profiles.yml`.

If two of these disagree, dbt-refmerge refuses with `ADAPTER_MISMATCH` rather than analyze SQL in the wrong dialect. If none is available, it refuses with `UNSUPPORTED_ADAPTER`. In CI, where `profiles.yml` is often absent, set `adapter` in `.dbt-refmerge.toml` or pass `--adapter`.
