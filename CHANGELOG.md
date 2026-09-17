# Unreleased
- `check` verifies every model in one batch: a fixed number of dbt invocations (one candidate compile,
  one harness parse and run, one catalog query, one comparison call, one drop) however many models are
  checked. Failures stay per model: a broken candidate compile is retried alone, a view dbt cannot build
  fails only its model, and a failing comparison is re-run alone.
- `check` and `fix` print progress lines on stderr in human mode (not with `--json`).

# 0.1.0 (2026-09-16)
- `scan` lists duplicate import CTEs from source; `check` proves each merge on PostgreSQL (two scratch views,
  exact column types, one-statement multiset comparison, views dropped afterwards); `fix` re-proves and
  applies one model atomically; `cleanup --run-id` drops what an interrupted check left behind.
- Fail-closed safety kernel: dialect-aware parsing, delta gate on the compiled SQL, volatility gate,
  nested-CTE shadowing and relation checks, symlink-safe snapshot and apply.
- `--select` takes dbt selectors; `fix --dry-run` shows the diff; `--fail-on` is a minimum severity;
  `--json` output for every command; `dbt-refmerge --version`.
- Test suite at 100% line and branch coverage across unit, fake-dbt and PostgreSQL lanes.
