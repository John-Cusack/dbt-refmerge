# Unreleased
- `scan`, `check` and `fix` narrow direct `ref()`/`source()` import wildcards and remove unused plain
  import columns when the final query names its outputs. Requirements include joins, filters,
  calculations and every consumer, and are traced through single-source wildcard CTEs. Single imports
  are supported; narrowed duplicate imports can merge in the same rewrite, including single-line
  projection lists. Ambiguous and schema-dependent SQL is left alone. `scan` reports
  `UNUSED_IMPORT_COLUMNS`; compiled-delta and PostgreSQL equality checks also guard pruning.
- Text-only Snowflake and BigQuery pruning covers import `EXCLUDE`/`EXCEPT`, quoted identifiers,
  qualified nested fields and `UNNEST`/lateral `FLATTEN` inputs. BigQuery quoted column/query aliases
  bind case-insensitively. Dialect fixtures run without warehouse connections; `check`/`fix` remain
  PostgreSQL-only.

# 0.2.0 (2026-09-17)
- `check` verifies every model in one batch: a fixed number of dbt invocations (one candidate compile,
  one harness parse and run, one catalog query, one comparison call, one drop) however many models are
  checked. Failures stay per model: a broken candidate compile is retried alone, a view dbt cannot build
  fails only its model, and a failing comparison is re-run alone.
- `check` and `fix` print progress lines on stderr in human mode (not with `--json`).
- `scan --format github` prints GitHub Actions annotations; `--format text|json|github` (`--json` is
  `--format json`). Text output is now `path:line: message`, and JSON findings carry `line`.
- `scan` no longer reports a model its CTE parser cannot read unless the model names the same relation in two
  literal `ref()`/`source()` calls, so `--fail-on finding` doesn't fail on unrelated models.
- A pre-commit hook (`dbt-refmerge-scan`) and a composite GitHub Action (`uses: John-Cusack/dbt-refmerge@…`).
- `python -m dbt_refmerge` runs the CLI.
- `check` no longer marks a model `unverifiable` for being incremental, ephemeral or unparseable unless it may import a
  relation twice; such models are `not_run` with `NO_DUPLICATE_IMPORT`, so they don't fail `--fail-on`.
- User documentation in `docs/`: CLI reference and exit codes, configuration, verification, every reason code,
  JSON output and integrations, kept in sync with the code by tests.
- Releases publish a GitHub release with the wheel, the sdist and a CycloneDX SBOM; PyPI uploads carry
  attestations.

# 0.1.0 (2026-09-16)
- `scan` lists duplicate import CTEs from source; `check` proves each merge on PostgreSQL (two scratch views,
  exact column types, one-statement multiset comparison, views dropped afterwards); `fix` re-proves and
  applies one model atomically; `cleanup --run-id` drops what an interrupted check left behind.
- Fail-closed safety kernel: dialect-aware parsing, delta gate on the compiled SQL, volatility gate,
  nested-CTE shadowing and relation checks, symlink-safe snapshot and apply.
- `--select` takes dbt selectors; `fix --dry-run` shows the diff; `--fail-on` is a minimum severity;
  `--json` output for every command; `dbt-refmerge --version`.
- Test suite at 100% line and branch coverage across unit, fake-dbt and PostgreSQL lanes.
