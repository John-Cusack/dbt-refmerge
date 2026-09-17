# Improvement Plan after 0.1.0

dbt-refmerge 0.1.0 shipped to PyPI on 2026-09-16 with 100% line and branch coverage. This plan lists what to add next. It draws on three sources:

- **Release follow-ups:** Codecov, branch protection, and verification speed.
- **Research:**
  - the [OpenSSF Scorecard checks](https://github.com/ossf/scorecard/blob/main/docs/checks.md)
  - GitHub Actions hardening with [zizmor](https://docs.zizmor.sh/)
  - how comparable dbt tools reach projects: pre-commit hooks ([dbt-checkpoint](https://github.com/dbt-checkpoint/dbt-checkpoint), [SQLFluff](https://docs.sqlfluff.com/en/latest/production/pre_commit.html)) and GitHub PR annotations (SQLFluff's `github-annotation-native` format)
- **Code gaps** found while building 0.1.0.

Items marked **Now** are implemented in this round, each with the tests or checks listed. **Later** items are deliberately deferred, with the reason. **Maintainer-only** steps need an account or decision that code can't provide.

## Status

| ID | Item | Tier | Status |
|---|---|---|---|
| A1 | Branch protection on `main` | Now | |
| A2 | Pin every GitHub Action to a commit SHA, on current majors | Now | |
| A3 | Dependabot for Actions and Python dependencies | Now | |
| A4 | CodeQL static analysis (Python and Actions) | Now | |
| A5 | zizmor audit of the workflows | Now | |
| A6 | OpenSSF Scorecard workflow and badge | Now | |
| A7 | SECURITY.md, CONTRIBUTING.md, issue and PR templates, CODEOWNERS | Now | |
| A8 | Refresh `.pre-commit-config.yaml` | Now | |
| A9 | Private vulnerability reporting | Now | |
| B1 | Release workflow publishes the GitHub release, with dists and an SBOM | Now | |
| C1 | pre-commit hook for dbt projects | Now | |
| C2 | `scan --format github` PR annotations | Now | |
| C3 | Reusable GitHub Action | Now | |
| C4 | `python -m dbt_refmerge` | Now | |
| D1 | User documentation in `docs/`, checked by tests | Now | |
| E1 | Batched verification: constant dbt invocations per `check` | Now | |
| E2 | Progress messages for `check` and `fix` | Now | |
| F1 | Property-based rewrite test | Now | |

## Now

### A. Repository and supply chain

**A1. Branch protection on `main`.**
- **Why:** CI is only a guarantee if a red check blocks the merge (Scorecard *Branch-Protection*).
- **What:** require these checks: the six `gates` jobs, `lowest-deps` and `coverage`. Block force pushes and branch deletion.
- **No required reviews:** there is one maintainer, and required reviews would block every merge. Revisit once a second maintainer joins.
- **Done when:** the branch protection API shows the rule on `main`.

**A2. Pinned actions.**
- **Why:** a moved tag can swap the code a workflow runs, including the job that publishes to PyPI (Scorecard *Pinned-Dependencies*).
- **What:** every `uses:` names a full commit SHA with a `# vX.Y.Z` comment, moved to current majors: checkout v7, setup-python v7, upload-artifact v7, download-artifact v8, gh-action-pypi-publish v1.14, codecov v7. Dependabot (A3) keeps the SHAs current.
- **Done when:** zizmor (A5) reports no unpinned uses.

**A3. Dependabot.**
- **What:** weekly updates for `github-actions` and for `pip`, read from `pyproject.toml`, grouped so each ecosystem opens one PR a week (Scorecard *Dependency-Update-Tool*).

**A4. CodeQL.**
- **What:** Python and GitHub Actions analysis on pull requests, on pushes to `main` and weekly (Scorecard *SAST*). Results go to code scanning.

**A5. zizmor.**
- **What:** audit the workflows for injection, over-broad permissions and credential persistence on pull requests and pushes (Scorecard *Dangerous-Workflow*). SARIF results go to code scanning.
- **Done when:** a clean run on this repository.

**A6. OpenSSF Scorecard.**
- **What:** a weekly and push-to-`main` Scorecard run that publishes results, plus the Scorecard badge in the README.

**A7. Community files.**
- **`SECURITY.md`:** private reporting through GitHub, supported versions, and what counts as a vulnerability. For this tool that includes a false "equivalent" verdict, a write outside the model file, or a scratch object outside the scratch schema.
- **`CONTRIBUTING.md`:** the three test lanes, the gates, the no-mocks conventions and the release steps.
- **Issue templates:** a bug report that asks for the dbt and adapter versions, the command, and `--json` output with reason codes; plus a feature request.
- **Also:** a pull request checklist and `CODEOWNERS`.

**A8. pre-commit refresh.**
- **Why:** `.pre-commit-config.yaml` still pins ruff 0.4 and a mypy mirror without the project's dependencies, so hooks disagree with CI.
- **What:** use current ruff, and run mypy through the project's own environment (a `local` hook).

**A9. Private vulnerability reporting.**
- **What:** enable GitHub's private vulnerability reporting, which `SECURITY.md` points to.

### B. Release

**B1. GitHub release from the workflow.**
- **Why:** 0.1.0's GitHub release was created by hand.
- **What:** after the PyPI upload, the release workflow creates the release for the tag, with notes taken from that version's CHANGELOG section. It attaches the wheel, the sdist and a CycloneDX SBOM of the runtime dependencies (Scorecard *SBOM*). PyPI already carries PEP 740 attestations (*Signed-Releases*).
- **Done when:** the notes extraction (`scripts/release_notes.py`) is unit-tested, including a check that the current version has notes; the SBOM command is verified locally; and actionlint and zizmor pass on the workflow.

### C. Reaching dbt projects

**C1. pre-commit hook.**
- **What:** `.pre-commit-hooks.yaml` exposes `dbt-refmerge-scan`, which runs `dbt-refmerge scan --fail-on finding`. Projects can block new duplicate imports at commit time, the way dbt-checkpoint and SQLFluff hooks are used.
- **Done when:** `pre-commit try-repo` runs the hook against a sample project.

**C2. `scan --format github`.**
- **What:** emit GitHub workflow commands (`::warning file=…,line=…,title=…::…`), one per lead, at the line of the first duplicated CTE, so leads appear inline on pull requests.
- **Flag changes:** `--format text|json|github`. `--json` stays as a shorthand for `--format json`.
- **Done when:** unit tests cover escaping of `%`, `:` and `,` in paths and messages, and the line numbers.

**C3. Reusable GitHub Action.**
- **What:** a composite `action.yml` in the repository root that installs dbt-refmerge at the requested version and runs `scan --format github` with a configurable project directory and adapter.
- **Done when:** a CI job runs the action from the checkout against a sample project and checks the annotation output.

**C4. `python -m dbt_refmerge`.**
- **Why:** works where console scripts are not on `PATH`.
- **Done when:** a test runs it.

### D. Documentation

**D1. `docs/`.**
- **Pages:**
  - `cli.md`: every command, option and exit code.
  - `configuration.md`: `.dbt-refmerge.toml` keys, `DBT_REFMERGE_*` variables and precedence.
  - `verification.md`: what `check` creates, the privileges it needs, the scratch schema, cleanup and limits.
  - `reason-codes.md`: every `ReasonCode`, what it means, and what to do about it.
  - `json-output.md`: the output of `scan`, `check`, `fix` and `cleanup`, with `schema_version`.
- **README:** links to these pages.
- **Done when:** unit tests fail if a `ReasonCode` is missing from `reason-codes.md`, or if a CLI option or command is missing from `cli.md`, so the docs can't drift.

### E. Performance and UX

**E1. Batched verification.**
- **Why:** each dbt invocation costs 2–3 s of startup. Today `check` spends a candidate compile, a parse, a run, two queries and a drop per model, about 15 s each. A 30-model project takes minutes.
- **Invocations per `check`, independent of model count:**
  1. `--version`
  2. `compile --help`
  3. the baseline compile
  4. one candidate compile selecting every planned model (`path:` selectors)
  5. one harness `parse`
  6. one harness `run`
  7. one catalog query
  8. one verdict run-operation that runs every model's comparison and prints each result under its own marker
  9. one drop-and-confirm
- **Keeping failures per model:**
  - A failed batch candidate compile is retried one candidate at a time, with the other models restored to their original source. dbt parses the whole project, so a parse error in one candidate would otherwise fail every compile, with no per-model results.
  - A failed harness run is attributed to models through `run_results.json`. Models that succeeded carry on.
  - Two models whose scratch view names would collide (the same 8-hex-digit hash) are refused, so neither is judged on the other's SQL.
  - If the batched verdict run-operation fails, each model's comparison is re-run on its own, so one bad query cannot hide the others' verdicts.
  - A preflight violation still refuses the whole batch: the harness project is not what was written.
- **Done when:**
  - fake_dbt tests cover partial compile, run and verdict failures.
  - warehouse tests prove several models in one `check`.
  - a timing check shows the invocation count no longer grows with the number of models.

**E2. Progress messages.**
- **What:** `check` and `fix` print short stage lines to stderr in human mode (compiling the project, preparing N merges, verifying on the warehouse, cleaning up). JSON output and stdout are untouched.

### F. Test strength

**F1. Property-based rewrite test.**
- **What:** Hypothesis generates models with two or three import CTEs over one `ref()`, with random disjoint or overlapping projections, predicates and downstream references. The properties to hold:
  - the planner either refuses with a reason code or produces a candidate that reparses;
  - the candidate has exactly one import for the ref;
  - the union of projections is preserved;
  - the compiled-delta gate accepts it.
- **Why:** plan §8 of `TEST_COVERAGE_PLAN.md` notes this property would have caught B3.

## Later

- **Verification on other adapters.** DuckDB is the natural second adapter: it is local and testable in CI, and supports `EXCEPT ALL`, schemas and views. It still needs its own exact-type allowlist, catalog query and file-locking handling, because dbt-duckdb holds the database file. Snowflake and BigQuery need accounts, cost controls (a query tag, a byte cap) and their own type rules. Each adapter is a safety-critical project with its own plan.
- **Mutation testing** (for example mutmut) on `semantics`, `rewrite`, `source` and `verification/*`. It takes hours of compute plus triage of surviving mutants, and is better after E1 settles.
- **Fuzzing** of the source frontend (Atheris or ClusterFuzzLite; Scorecard *Fuzzing*). The Hypothesis properties (F1) cover the rewrite first.
- **OpenSSF Best Practices badge** (Scorecard *CII-Best-Practices*). A maintainer questionnaire; most answers become "yes" after this round.
- **A docs website** (for example MkDocs). Markdown under `docs/` renders on GitHub and is enough until there are more pages.
- **dbt Fusion engine and dbt Cloud CLI compatibility.** Different executables and flags. Revisit when users ask.

## Maintainer-only

- **Codecov:** sign in at codecov.io with GitHub and add the repository. CI already uploads through OIDC, and the badge shows "unknown" until then.
- **Scorecard publishing:** runs without setup, but the badge appears only after the first run on `main`.
- **Releases:** approving the `pypi` environment for each tag stays a human step.
