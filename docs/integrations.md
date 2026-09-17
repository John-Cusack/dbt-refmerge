# Integrations

`scan` needs no dbt run and no warehouse, so it fits in commit hooks and pull request checks. Keep `check` and `fix` for a machine that can reach a development database.

The pre-commit hook, the GitHub Action and `scan --format github` need dbt-refmerge 0.2.0 or later.

`scan` must know the SQL dialect. On CI machines without a `profiles.yml`, set it once in the project:

```toml
# .dbt-refmerge.toml
adapter = "snowflake"
```

## pre-commit

The repository ships a [pre-commit](https://pre-commit.com) hook, `dbt-refmerge-scan`. It runs `dbt-refmerge scan --fail-on finding` whenever a `.sql` file changes, and fails when any model imports the same `ref()` or `source()` in more than one CTE.

```yaml
# .pre-commit-config.yaml
repos:
  - repo: https://github.com/John-Cusack/dbt-refmerge
    rev: v0.2.0 # a release tag; the hook needs 0.2.0 or later
    hooks:
      - id: dbt-refmerge-scan
        # When the dbt project isn't the repository root, or the adapter isn't configured:
        args: [--project-dir, transform, --adapter, snowflake]
```

The hook reads the whole project rather than the changed files, because a lead depends on the project's `model-paths` and manifest.

## GitHub Actions

The repository is also a composite action. It annotates pull requests at the first duplicated import of each lead:

```yaml
# .github/workflows/dbt-refmerge.yml
name: dbt-refmerge
on: pull_request
permissions:
  contents: read
jobs:
  scan:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
        with:
          persist-credentials: false
      - uses: John-Cusack/dbt-refmerge@v0.2.0 # pin a release tag, or a commit SHA
        with:
          project-dir: transform
          adapter: snowflake
          fail-on: finding # omit to annotate without failing the job
```

| Input | Default | Meaning |
|---|---|---|
| `project-dir` | `.` | The dbt project, relative to the repository root. |
| `adapter` | from configuration | The dialect; see [adapters](configuration.md#adapters). |
| `fail-on` | `never` | `finding` fails the step when there are leads. |
| `package` | `dbt-refmerge` | The pip requirement to install, e.g. `dbt-refmerge==0.2.0`. |
| `python-version` | `3.12` | The Python that runs dbt-refmerge. It is installed alongside, and doesn't change the job's own Python. |

| Output | Meaning |
|---|---|
| `findings` | The number of leads (annotations). |

The action installs dbt-refmerge into its own virtual environment and runs `scan --format github`.

## Other CI systems

Any CI system can run `scan` directly:

```bash
pip install dbt-refmerge
dbt-refmerge scan --project-dir transform --fail-on finding          # exit 2 when there are leads
dbt-refmerge scan --project-dir transform --json > dbt-refmerge.json   # for your own reporting
```

`--format github` works anywhere that understands GitHub workflow commands. [JSON output](json-output.md) describes the document for everything else.
