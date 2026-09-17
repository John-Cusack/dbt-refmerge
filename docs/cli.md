# Command-line reference

```
dbt-refmerge [--version] COMMAND [OPTIONS]
python -m dbt_refmerge COMMAND [OPTIONS]
```

`python -m dbt_refmerge` runs the same CLI. Use it where the `dbt-refmerge` console script is not on `PATH`.

| Command | What it does | Needs dbt | Needs a warehouse | Edits files |
|---|---|---|---|---|
| [`scan`](#scan) | Lists duplicate import CTEs, read from the model source files | no | no | no |
| [`check`](#check) | Rewrites each model in a private copy and proves the merge on the warehouse | yes | yes | no |
| [`fix`](#fix) | Re-proves one model's merge and writes it only if the proof passes | yes | yes | one model file |
| [`cleanup`](#cleanup) | Drops scratch views an interrupted `check` left behind | yes | yes | no |

`--profiles-dir`, `--profile`, `--target`, `--adapter`, `--dbt-command-part`, `--scratch-schema`, `--json`, `--fail-on`, `--keep-workspace` and `--debug` can also be set in `.dbt-refmerge.toml` or a `DBT_REFMERGE_*` environment variable; see [configuration](configuration.md). A flag you don't pass never overrides those. `--project-dir`, `--format`, `--select`, `--dry-run`, `--run-id` and the model path come only from the command line.

## Options shared by several commands

| Option | Commands | Meaning |
|---|---|---|
| `--project-dir PATH` | all | The dbt project (the directory holding `dbt_project.yml`). Default: the working directory. |
| `--profiles-dir PATH` | all | Where `profiles.yml` lives. Default: dbt's own lookup (`DBT_PROFILES_DIR`, then the project directory, then `~/.dbt`). |
| `--profile NAME` | all | The dbt profile. Default: `profile:` in `dbt_project.yml`. |
| `--target NAME` | all | The profile target. Default: the profile's `target:`. |
| `--adapter NAME` | `scan`, `check`, `fix` | The warehouse, which sets the SQL dialect. Default: read from the compiled manifest or `profiles.yml`. See [adapters](configuration.md#adapters). |
| `--dbt-command-part PART` | `check`, `fix`, `cleanup` | The dbt executable, one argument per flag, e.g. `--dbt-command-part uv --dbt-command-part run --dbt-command-part dbt`. Default: `dbt`. |
| `--scratch-schema NAME` | `check`, `fix`, `cleanup` | The schema that receives the verification views. Required for these commands. See [verification](verification.md). |
| `--json` | `scan`, `check`, `fix` | Machine-readable output on stdout; see [JSON output](json-output.md). (`cleanup` always prints JSON.) |
| `--fail-on LEVEL` | `scan`, `check` | The minimum severity that makes the exit code non-zero; see [exit codes](#exit-codes). |
| `--keep-workspace` | `check`, `fix` | Keep the private working copy (snapshot, candidate files, dbt artifacts) and print its path. |
| `--debug` | all | Print the Python traceback with any error. |

## scan

```
dbt-refmerge scan [--project-dir PATH] [--adapter NAME] [--format text|json|github] [--json] [--fail-on LEVEL]
                  [--profiles-dir PATH] [--profile NAME] [--target NAME] [--debug]
```

Reads every `.sql` file under the project's `model-paths`. It reports a lead for each model where two or more CTEs import the same `ref()` or `source()`. No dbt command runs and nothing connects to a warehouse. If `target/manifest.json` exists, `scan` uses it to name the upstream model.

A model whose CTE list `scan` can't parse (for example `WITH RECURSIVE`, or a CTE column list) is reported only when it names the same relation in two literal `ref()`/`source()` calls. It is reported with reason `UNSUPPORTED_IMPORT_SHAPE`.

`--format` chooses the output:

- `text` (default): one `path:line: message` line per lead, with paths relative to the project.
- `json`: the [scan document](json-output.md#scan). `--json` is the same as `--format json`, and combining `--json` with another `--format` is refused.
- `github`: one GitHub Actions `::warning` workflow command per lead, at the line of the first duplicated import. Paths are relative to `GITHUB_WORKSPACE`, or to the working directory outside Actions. See [integrations](integrations.md).

`--fail-on finding` exits 2 when there is at least one lead. `scan` ignores every other level.

## check

```
dbt-refmerge check --scratch-schema NAME [--select SELECTOR] [--project-dir PATH] [--adapter NAME] [--json]
                   [--fail-on LEVEL] [--keep-workspace] [--dbt-command-part PART ...]
                   [--profiles-dir PATH] [--profile NAME] [--target NAME] [--debug]
```

1. Copies the project into a private workspace and compiles it with dbt.
2. Rewrites each model that has duplicate imports, in the copy only.
3. Compiles the rewritten models, and refuses any whose compiled SQL changed in a way the merge doesn't explain.
4. Proves the rest on the warehouse: it builds the original and the rewritten SQL as two views per model, compares their column types and their rows as multisets, then drops the views.

All models share one batch of dbt calls. [Verification](verification.md) covers the details.

`--select SELECTOR` takes any dbt selector (`orders`, `tag:finance`, `path:models/marts`). By default every model is checked.

In human mode, progress lines go to stderr and the per-model report goes to stdout. The report includes the diff for every model that reached the warehouse. With `--json`, stdout carries the [check document](json-output.md#check) and stderr is only used for errors.

## fix

```
dbt-refmerge fix MODEL_PATH --scratch-schema NAME [--dry-run] [--project-dir PATH] [--adapter NAME] [--json]
                 [--keep-workspace] [--dbt-command-part PART ...]
                 [--profiles-dir PATH] [--profile NAME] [--target NAME] [--debug]
```

Runs `check` for the one model at `MODEL_PATH`, which may be absolute, relative to the working directory, or relative to the project. If the merge is proven and the scratch views are gone, `fix` writes the rewritten file atomically. It first confirms the file still has the bytes that were proven; if not, it refuses with `SOURCE_CHANGED_BEFORE_APPLY`.

`--dry-run` proves the merge and prints the diff without writing.

The human output ends with `applied=<bool> dry_run=<bool> <reason>`, where the reason is one of:

- `applied`
- `dry-run`
- `not fixable`
- `model not found`

`applied=false` with a reason is a refusal, not a crash.

## cleanup

```
dbt-refmerge cleanup --run-id RUN_ID --scratch-schema NAME [--project-dir PATH] [--dbt-command-part PART ...]
                     [--profiles-dir PATH] [--profile NAME] [--target NAME] [--debug]
```

Drops the views that the `check` run with `RUN_ID` may have left in the scratch schema. That happens if the run was killed, the machine lost power, or the drop itself failed. The run id is in `check --json` (`run_id`) and has the form `20260916T120000_0123456789ab`.

Only views whose names match that run's naming pattern are dropped. Tables, and views from other runs, are never touched. `cleanup` always prints the [cleanup document](json-output.md#cleanup).

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success, or nothing at or above `--fail-on` |
| 1 | Operational error: bad configuration, dbt failed on the project, model not found, cleanup left views behind |
| 2 | Policy: a lead (`scan`, or `check --fail-on finding`) or a proven, fixable merge (`check --fail-on fixable`) |
| 3 | A merge changed the model's results (`different`) |
| 4 | A merge could not be verified (`unverifiable` or `error`) |
| 130 | Interrupted (Ctrl-C) |

`check` ranks each model's result, from least to most severe: finding, fixable, different, unverifiable. `--fail-on LEVEL` fails the run when any result ranks at or above `LEVEL`, and the exit code comes from the most severe result:

| Result | `never` | `finding` | `fixable` (default) | `different` | `unverifiable` |
|---|---|---|---|---|---|
| no duplicate imports | 0 | 0 | 0 | 0 | 0 |
| a lead that was not verified (any other `not_run`) | 0 | 2 | 0 | 0 | 0 |
| proven and fixable (`snapshot_equivalent`) | 0 | 2 | 2 | 0 | 0 |
| `different` | 0 | 3 | 3 | 3 | 0 |
| `unverifiable` or `error` | 0 | 4 | 4 | 4 | 4 |

`fix` exits:

- 0 when the merge was applied, or on `--dry-run` when it could be;
- 1 when the model isn't found or an operational error occurs;
- 3 when the merge changed the results;
- 4 for any other refusal.
