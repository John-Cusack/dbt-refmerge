# Test Coverage Plan: 60% → 100%

Measured 2026-09-16 on `acf0184` (the PR #4 branch; production code matches `main` apart from the `--version` flag) with Python 3.12, sqlglot 30.18.0 and `pytest --cov` (line and branch). Line numbers refer to that commit.

## Summary

- **Coverage is 60%:** 2,962 statements with 1,060 missed, 1,170 branches with 166 partial, from 128 tests.
  - The SQL front end and rewriter are well covered (61–100%).
  - The code that runs dbt, verifies, reports, and writes files is not (16–36%).
- **100% is reachable without mocks.** Throwaway probe tests written during this review (about 150, not committed) took the suite to **90%** without changing production code. The last 10% is:
  - dead code, deleted
  - unreachable defensive branches, turned into `assert`s or removed
  - platform-specific arms, handled by platform pragmas
  - the warehouse lane
- **The review found the core promise isn't implemented** (§2.1):
  - Nothing wires a verifier, so `check` never proves a merge, `fix` never writes, and `cleanup` never drops anything.
  - Several bugs stop `check` before verification would even run.

  Tests that pin today's behavior would lock that in, so the plan fixes these first, test-first.
- **Decision (2026-09-16): build the verifier** (§5.1, option A). Don't publish to PyPI until Phase 5 is done.
- **Estimated work:**
  - About 180 new test functions (about 320 parametrized cases).
  - Roughly 100–150 statements of dead or defensive code removed.
  - Three test lanes.
  - One CI coverage job with a Postgres service.

## Progress

| Phase | Status | PR | Coverage after |
|---|---|---|---:|
| 0 | done | #5 | 61.2% (`fail_under = 61`) |
| 1 | done (option A) | — | — |
| 2 | done: B2–B7, S1–S11 | #6 | 71.3% (`fail_under = 71`) |
| 3 | done: unit lane for reporting, config, errors, domain, analyze, rewrite, semantics, source, artifacts, adapters, workspace, apply | #7 | 85% (`fail_under = 85`) |
| 4 | done: dbt_cli, CLI and orchestrator in the fake_dbt lane; remaining verifier helpers; CLI/orchestrator bugs from §2.3 | #9 | **100%** (`fail_under = 100`) |
| known limits | done: Jinja string tokens, raw whitespace control, sentinel names, dbt version semantics, ORDER BY coverage, linked local packages, faster cleanup and warehouse tests | Known-limits PR | 100% |
| 5 | verifier built (B1) and warehouse lane added; phase order swapped with 4 so CLI tests target final behavior | #8 | 90% (`fail_under = 90`) |

Discrepancies found while implementing:

- **Phase 0:** the Postgres DSN variable is `REFMERGE_TEST_PG_DSN`, not `DBT_REFMERGE_TEST_PG_DSN`. `load_config` reads every `DBT_REFMERGE_*` variable as config, and the `isolated_env` fixture scrubs that prefix.
- **Phase 0:** covdefaults excludes `if __name__ == "__main__":`, so the starting total is about 61% rather than 60%.
- **B2 confirmed against real dbt-core 1.12.5:** `dbt --version` prints `Core:` on its own line.
- **Phase 2, B4:** confirmed by the new two-group test: it failed with `COMPILE_DRIFT` once B3 was fixed.
- **Phase 2, S1 scope:** `LIMIT`, `OFFSET`, `FETCH` and `DISTINCT ON` are refused only when their own query level has no `ORDER BY`. An `ORDER BY` with ties still passes the static gate. The warehouse comparison is the backstop, and this should be revisited when the verifier lands.
- **Phase 2, S3:** the harness refuses any `{%` in compiled SQL, not just `endraw` spellings.
- **Phase 2, S6:** verdict SQL requires fully quoted `"db"."schema"."name"` relations, and every CTE and helper column is namespaced `__dbt_refmerge_*`. This also removes the grouped-counts `_a`/`_b`/`_delta` collision.
- **Phase 2, S7:** each `SemanticImport` now carries the normalized compiled relation, and `qualify_group` refuses a group whose members read different relations (`SOURCE_MAPPING_AMBIGUOUS`).
- **Phase 2, S9:**
  - A lock failure raises `RefmergeError(INTERNAL_ERROR)`.
  - The lock file is opened with `O_NOFOLLOW`, never truncated, and removed afterwards (before closing on POSIX, after closing on Windows).
  - The source is re-hashed unconditionally before `os.replace`.
- **Phase 2, S11:** the launch-failure `DbtError` also carried an unredacted argv; now fixed.
- **Phase 3, work split:** five parallel agents, one per module group, then integrated with reviewer fixes. Every module named in §6.1–§6.4 is at 100% line and branch coverage in the unit lane.
- **Phase 3, `--fail-on` semantics:** `fail_on` is now the minimum failing severity, ordered finding < fixable < different < unverifiable. So `--fail-on unverifiable` with a `different` result exits 0, contrary to the second example in §2.3. `--fail-on different` now exits 4 when an unverifiable result is present.
- **Phase 3, fixes found beyond the plan:**
  - **Artifacts:** manifests that are missing, not UTF-8, deeply nested, or have over-long integers raise `ArtifactError`. So do non-object nodes and sources, a node whose key differs from its `unique_id`, and `refs`/`sources` metadata that contradicts every candidate.
  - **Adapters:** profiles resolve like dbt: `DBT_PROFILES_DIR`, then `./profiles.yml`, then `~/.dbt`; the target defaults to `default`; `profiles.yaml` is not read.
  - **Workspace:**
    - `target`, `logs`, `.git`, `node_modules`, `.venv` and `venv` are pruned only at the project root.
    - `dbt_packages` is copied.
    - Linked directories and symlink loops are refused.
    - `cleanup_files` reports failures, and `check` tolerates a leftover local temp dir.
  - **Config:** `dbt_command` strings use `shlex.split`. Relative `--profiles-dir` and `DBT_PROFILES_DIR` are resolved up front.
  - **Semantics:** the volatility allowlist is wired in (deterministic, aggregate and STABLE functions pass; volatile, order-sensitive and unknown functions refuse), and non-RANGE/GROUPS window frames refuse.
  - **Source:** clauses after `WHERE` clear the import shape. Nested CTE shadowing is now detected everywhere; before, a donor referenced through a nested CTE of the same name could be merged and pass the delta gate. Unknown call kwargs and expression projections are no longer literal imports.
- **Phase 3, pragma budget (§7) changes:**
  - Added `# pragma: >=3.12 cover` / `<3.12 cover` for the `rmtree` callback split.
  - `orchestrator.py` now has four win32 pragmas (S9 added one).
  - `semantics.py` 192 is tested instead of excluded.
  - Two rewrite separator guards stay typed refusals with `# pragma: no cover` rather than asserts, because asserts vanish under `-O` and abort the run.
- **Phase 3, known gaps (all resolved in the known-limits PR):**
  - Jinja strings containing `}}` or `%}` ended a tag early. Tag ends now follow Jinja's string tokens.
  - `{%- raw %}` was not recognized. Whitespace control is accepted on both raw tags.
  - Sentinel-looking names (`__r1__`) in user SQL could create false leads. Such SQL is refused.
  - `version=0` or `''` counted as versioned. They now follow dbt's `version or v`, and a boolean version is dynamic.
  - Local `dbt deps` packages installed as symlinks were refused. Links directly in `dbt_packages` are copied, with the same refusals inside the package.
- **Phase 5, verifier as built** (validated against dbt-core 1.12.5 and dbt-postgres 1.11 on postgres:16). Differences from the §5.1 sketch:
  - The harness `generate_schema_name` reads the scratch schema from `--vars`, so the name is never embedded in Jinja.
  - Every query goes through one `dbt_refmerge_query` run-operation macro. The SQL is passed via `--args`, which dbt does not render, and values come back as strings between nonce markers.
  - Views are aliased per run and per model. `cleanup --run-id` finds leftovers by that name pattern in the scratch schema, so no ledger file has to outlive the temp workspace.
  - Column types come from `pg_catalog` (`format_type`), not `adapter.get_columns_in_relation`, which loses typmods.
  - The cleanup macro drops only views, by exact name. A table carrying one of our names is left alone.
- **Phase 5, known limits:**
  - ~~`ORDER BY` with ties passes the static volatility gate.~~ Resolved: `LIMIT`/`OFFSET`/`FETCH`/`DISTINCT ON` need an `ORDER BY` covering every output column.
  - Verification runs sequentially, one model at a time. Each model costs a candidate compile, then parse, run, two queries, and one drop-and-confirm call, at about 2–3 s of dbt startup each. Batching every model into one harness project would make the cost roughly constant; that is possible future work, not a correctness gap.
- **Phase 4 (after the verifier), CLI behaviour changes:**
  - Unset flags no longer override config.
  - Output is plain text: no Rich markup, no wrapping.
  - `fix --dry-run` prints the diff.
  - `fix` exits 0 when it applies or when a dry run is fixable, 3 when the proof found a difference, and 4 otherwise.
  - Ctrl-C exits 130, and `--debug` prints the traceback.
  - Removed `scan --select`/`--compile` and `--allow-compile-introspection`. `scan --fail-on finding` exits 2 when there are leads.
  - `check --json` reports the dbt version, the manifest schema and the kept workspace.
- **Phase 4, orchestrator changes:**
  - `--select` is a dbt selector. Package models are never candidates.
  - `fix` resolves an exact project file and checks only that model.
  - Model discovery reads `model-paths`.
  - A candidate manifest that cannot be read refuses that model only.
  - A model source missing from the snapshot is refused. It used to fall back to a same-named file.
  - A manifest outside the workspace is never read.
- **Phase 4, `dbt_cli`:**
  - The child process is reaped on Ctrl-C and on timeout (SIGTERM, then SIGKILL after `terminate_grace_seconds`).
  - Logs are capped in bytes.
  - Output is decoded as UTF-8 with replacement.
  - One fewer `dbt --help` subprocess per check.

## 1. Where coverage stands

"Probes" is what the review's throwaway tests reached with no code changes. They show each gap is reachable, not that the work is done.

| Module | Cover | Missed stmts | Partial branches | Probes | What's untested |
|---|---:|---:|---:|---:|---|
| `reporting.py` | 16% | 63 | 0 | 70% | exit-code policy, JSON and human rendering |
| `cli.py` | 22% | 81 | 2 | 88% | every command body except `--help` / `--version` |
| `dbt_cli.py` | 22% | 155 | 0 | 72% | launch, timeout and kill, version parsing, argv; about 55 dead statements |
| `orchestrator.py` | 31% | 232 | 11 | 82% | `scan`/`check`/`fix`/`cleanup` bodies, atomic apply races |
| `verification/harness.py` | 32% | 65 | 4 | 99% | scratch-schema validation, harness project, manifest preflight |
| `config.py` | 35% | 67 | 0 | 80% | `load_config` entirely (TOML, env, validation) |
| `workspace.py` | 36% | 74 | 1 | 81% | snapshot walk (symlinks, races), package digest, cleanup |
| `verification/comparator.py` | 60% | 33 | 8 | 100% | schema normalization, marked-JSON parsing |
| `semantics.py` | 61% | 112 | 42 | 95% | compiled-SQL refusals, volatility, expected transform / delta gate |
| `artifacts.py` | 68% | 34 | 15 | 99% | manifest rejection paths, ref/source ambiguity |
| `adapters.py` | 81% | 15 | 14 | 100% | profile and project YAML edge cases |
| `source.py` | 81% | 104 | 51 | 94% | Jinja edge cases, CTE-list refusals, import-body shapes, downstream refs |
| `errors.py` | 83% | 6 | 0 | 97% | constructors of errors never raised in tests |
| `rewrite.py` | 91% | 14 | 11 | 94% | invariant guards, whitespace-only lines |
| `analyze.py` | 93% | 3 | 6 | 99% | collision dedupe, single-member upstreams |
| `domain.py` | 98% | 2 | 1 | 100% | span validation, `is_fixable` negatives |
| `__init__.py`, `capabilities.py` | 100% | 0 | 0 | 100% | — |
| **Total** | **60%** | **1,060** | **166** | **90%** | |

## 2. Findings to fix before (or while) writing tests

Coverage work reads every uncovered branch, so it doubles as a code review. *Reproduced* means a probe test or script demonstrated the problem. *By reading* means it follows from the code but wasn't executed.

### 2.1 Release blockers: verification never happens

The README says `check` "proves each merge on your warehouse" and `fix` "refuses to write unless the proof passes". Today no proof ever runs. Even if it did, `check` would refuse the README's own example.

| # | Where | Problem | Evidence |
|---|---|---|---|
| B1 | `cli.py:116,166,221,255`, `orchestrator.py:402-409` | `RefmergeService()` is built without `verify_runner`, and no runner exists in `src/`. Every eligible model ends `UNVERIFIABLE` / `INPUT_ISOLATION_UNAVAILABLE`, `fix` always reports "not fixable", and `cleanup` echoes an empty ledger. The macro templates at `harness.py:57-71` are `...` placeholders. `harness`, `comparator`, `DbtCli.run`, `run_operation` and `parse` have no production callers. | reproduced |
| B2 | `dbt_cli.py:180-185` | `version()` raises `IndexError` on dbt ≥ 1.5 output, which prints `Core:` on its own line. `check` and `fix` crash before compiling. | reproduced (fake dbt using the real format) |
| B3 | `semantics.py:373-376` (`_canonical`), tree built at `:668` | `_canonical` keeps `None`-valued args. A parsed `TableAlias` carries `columns=None` and the synthesized one doesn't. So every bare donor reference (`join b` → `join a as b`) fails the delta gate with `COMPILE_DRIFT`, including the README / golden `disjoint_projections` example. Fix: skip `None` args and bump `SEMANTIC_FINGERPRINT_VERSION`. | reproduced: with an injected verifier reporting equivalence, `fix` still returns `COMPILE_DRIFT` |
| B4 | `orchestrator.py:420-439` | Each group's expected tree applies only that group's merge but is compared against a candidate with every group merged. Models with two or more groups always get `COMPILE_DRIFT`. | by reading (masked by B3) |
| B5 | `orchestrator.py:423-431` | Passes a projection's output identity as SQL: `amount as amt` becomes `amt`, and `"Amount"` loses its quotes. Result: `COMPILE_DRIFT`. | reproduced |
| B6 | `semantics.py:653-668` | Renames every `exp.Table` whose bare name equals a donor, ignoring db/catalog. A donor named `stg` rewrites `db.sch.stg`, so the result is `COMPILE_DRIFT`. | reproduced |
| B7 | `orchestrator.py:329` | Source parsing sits outside any `try`. One model with `WITH RECURSIVE`, a CTE column list, or unterminated Jinja aborts the whole `check`. | reproduced |

### 2.2 Safety: routes to a false "equivalent" or an unsafe write

These don't bite today only because B1 means nothing is ever declared equivalent. They must be fixed before or with the verifier.

| # | Where | Problem | Evidence |
|---|---|---|---|
| S1 | `semantics.py:574-579` | The volatility gate misses several nondeterministic shapes. `tablesample` slips through because the code looks up `exp.Sample`, which sqlglot 30 doesn't define. It also misses `fetch first n rows` and `offset` without ORDER BY, `distinct on` without ORDER BY, and an unordered subquery LIMIT when an ORDER BY exists elsewhere. | reproduced (`tablesample`, `fetch first`) |
| S2 | `verification/comparator.py:161` | `bool(payload["schema_equal"])` turns `"false"` into `True`. Schema equality is the only signal for type-only changes and column reorders. Require `isinstance(value, bool)`. | reproduced |
| S3 | `verification/harness.py:115` | The `{% endraw %}` guard is exact-match. `{%endraw%}`, `{%- endraw %}` and `{% endraw -%}` also close the raw block, so compiled SQL can inject Jinja into the harness. | reproduced |
| S4 | `verification/harness.py:42` | The scratch-schema length limit is 128, but Postgres truncates identifiers at 63 bytes. `<63-char model schema>_x` passes the "must differ" check and lands in the model schema. | by reading |
| S5 | `verification/harness.py:31-36` | Malformed quoted names are accepted: `"abc` becomes `ab`, and `""` becomes an empty schema. | reproduced |
| S6 | `verification/comparator.py:97-138` | Relation refs are interpolated next to CTEs named `a`, `b`, `u`, `g`. An unqualified relation named `a` compares the baseline with itself, which is always equal. | by reading |
| S7 | `semantics.py:440-444` | Group members' compiled source tables aren't compared. A donor compiled from `db.other.unrelated` still groups as `MERGE_ELIGIBLE`. | reproduced (in memory) |
| S8 | `rewrite.py:117` | `build_plan` doesn't check `qg.status`. Given a `NOT_ELIGIBLE` group, it merges and drops the donor's predicate. The orchestrator filters today, so this is defense in depth. | reproduced (direct call) |
| S9 | `orchestrator.py:607-658` | Four problems with the apply lock and re-check: (1) the lock file is opened with `"w"`, which follows a planted symlink and truncates its target; (2) it is left behind as `<model>.sql.dbt-refmerge.lock`; (3) a `flock` failure proceeds unlocked; (4) content is re-hashed only if mtime or size changed. | reproduced (leftover lock); rest by reading |
| S10 | `artifacts.py:127-128` | Drive-letter paths (`C:\evil\m.sql`) pass the traversal checks. On Windows, the candidate write goes there. | reproduced (check); Windows write by reading |
| S11 | `dbt_cli.py:61-74`, `:160` | `_redact_argv` redacts nothing, and the `redact_mapping` result is discarded. | reproduced |

### 2.3 Correctness and UX

Fix these in the phase that covers their module.

- **`fix`:**
  - `orchestrator.py:446-452`: matches by string suffix (`a.sql` picked `models/data.sql`), and uses the only result when nothing matches. *reproduced*
  - `orchestrator.py:443`: runs `check` over the whole project to fix one file.
- **`--select`** (`orchestrator.py:501-505`): substring matching rather than dbt selection. `orders` matches `stg_orders`; `tag:x` matches nothing. *reproduced*
- **Flags that do nothing:**
  - `scan` ignores `--select`, `--compile` and `--fail-on`.
  - `--debug` and `--allow-compile-introspection` have no effect.
  - `ExitCode.INTERRUPTED` is never emitted.
  - `check --json` reports `dbt.version` and `manifest_schema_version` as `""`.
- **CLI overrides config** (`cli.py:76-80`): CLI defaults for `fail_on`, `json_output`, `debug`, `keep_workspace` and `allow_compile_introspection` always override TOML/env, and `DBT_REFMERGE_PROJECT_DIR` overrides `--project-dir`. *reproduced*
- **`--fail-on` hides failures** (`reporting.py:61-78`):
  - `--fail-on different` with results [different, unverifiable] exits 0.
  - `--fail-on unverifiable` with [different] exits 0.

  *reproduced*
- **Human output** (`cli.py`): Rich markup eats `[...]` in paths (`models/[legacy]/m.sql` prints as `models//m.sql`), and output hard-wraps at 80 columns off a TTY. *reproduced*
- **dbt subprocess** (`dbt_cli.py`):
  - `:151-153`: Ctrl-C leaves the dbt child unreaped. *reproduced*
  - `:156-159`: the log cap measures bytes but slices characters.
  - `:128`: no `encoding=`, so non-cp1252 output raises on Windows.
- **Config parsing** (`config.py`):
  - `:133`: `DBT_REFMERGE_DBT_COMMAND` is split with `str.split`, not `shlex.split`.
  - `:105`: `tool = "x"` in TOML raises `AttributeError`.
- **Workspace snapshot** (`workspace.py:20,87-99`):
  - Prunes `target`/`logs`/… at every depth, dropping `models/logs/*.sql`.
  - Never walks symlinked dirs.
  - Excludes `dbt_packages`, so projects with packages likely fail to compile in the snapshot.

  *first two reproduced; packages by reading*
- **Model discovery** (`orchestrator.py:111-118`): `discover_model_files` filters on absolute path parts, so a project under any `target/` directory finds no models. It also ignores `model-paths`. *reproduced*
- **Workspace retention and cleanup:**
  - `orchestrator.py:251,303`: `--keep-workspace` keeps `dbt_refmerge_<random>` but reports only `run_id`.
  - `workspace.py:196-200`: cleanup can't report failure (`ignore_errors=True`).
- **Manifest encoding** (`artifacts.py:97`): a non-UTF-8 manifest raises a bare `UnicodeDecodeError`, which crashes `scan`. *reproduced*
- **Over-refusal** (`semantics.py:51`): `DETERMINISTIC_FUNCTIONS` is never used, so `coalesce`, `lower`, `count`, `case` and `date_trunc` make a model `NONDETERMINISTIC`. *reproduced*
- **Scan status** (`source.py:711-721`): `where … group by` keeps `ref_call`, so `scan` says `NEEDS_COMPILED_ANALYSIS` instead of `UNSUPPORTED_IMPORT_SHAPE`. `check` still refuses.
- **Profiles** (`adapters.py:387`): falls back to target `dev` where dbt uses `default`, and ignores `DBT_PROFILES_DIR` and `./profiles.yml`.
- **Cleanup macro** (`verification/harness.py:73-78`): its `ref()` relation has no type, so dbt emits `drop None if exists`. *by reading dbt-adapters source*
- **Leaking test** (`tests/unit/test_harness_comparator.py:75-80`): `create(keep=True)` followed by `cleanup_files()` leaks one `/tmp/dbt_refmerge_*` dir per run (63 on the dev machine).

## 3. Definition of done

- `fail_under = 100` for line + branch coverage, enforced by one CI job that runs all three lanes.
- Exclusions are allowed only for:
  1. covdefaults' built-ins: `if TYPE_CHECKING:`, `...` bodies, `if __name__ == "__main__":`, `raise NotImplementedError`.
  2. Platform arms marked `# pragma: win32 cover` / `# pragma: win32 no cover`. The Windows CI job still executes them.
  3. Guards against sqlglot API changes, marked `# pragma: no cover` with a comment naming the invariant. §7 lists the allowed set; adding one needs a note in the PR.
- Dead code is deleted, not tested. Unreachable defensive branches become `assert`s or are removed.
- New tests follow the existing conventions:
  - plain functions, no classes, no mocks (`monkeypatch` for env/cwd/`tempfile.tempdir` and `tmp_path` are fine)
  - assertions on observable outcomes
  - byte-exact comparisons for rewrites
- `python3 -m pytest -q` stays sub-second, as the README promises. Slower lanes are opt-in locally.

## 4. Test infrastructure (Phase 0)

### 4.1 Lanes

| Lane | Selection | Location | Needs | CI |
|---|---|---|---|---|
| unit | default | `tests/unit`, `tests/golden`, `tests/property` | memory, `tmp_path`, in-process `CliRunner` | every matrix job |
| fake_dbt | `-m fake_dbt` | `tests/fake_dbt` | `tests/fakes/fake_dbt.py` run as a real subprocess | every matrix job (the fake is Python, so Windows exercises the win32 arms) |
| warehouse | `-m warehouse` | `tests/warehouse` | Postgres and `dbt-postgres` | ubuntu `coverage` job |

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
filterwarnings = ["error"]
addopts = ["--strict-markers", "-m", "not fake_dbt and not warehouse"]
markers = [
  "fake_dbt: runs against tests/fakes/fake_dbt.py in a subprocess",
  "warehouse: needs Postgres (REFMERGE_TEST_PG_DSN) and dbt-postgres",
]

[tool.coverage.run]
source = ["src/dbt_refmerge"]
plugins = ["covdefaults"]  # add covdefaults to the dev extra; it turns on branch coverage

[tool.coverage.report]
fail_under = 60  # ratchet: raise to the measured value in every coverage PR
```

A later `-m` on the command line overrides the one in `addopts`. `pytest -m ""` runs everything.

### 4.2 Fixtures (`tests/conftest.py`)

- **`isolated_env`** (autouse):
  - Deletes `DBT_REFMERGE_*`.
  - Points `HOME` and `USERPROFILE` at `tmp_path`. Adapter resolution reads `~/.dbt/profiles.yml` when no profiles dir is given, so some existing adapter tests can read the developer's real profile today.
  - Sets `tempfile.tempdir` to `tmp_path`, so workspaces can't leak into `/tmp`.
- **`dbt_project`**: builds a project in `tmp_path` (`dbt_project.yml`, models, optional `manifest.json`).
- **`fake_dbt`**: returns the `dbt_command` tuple and a helper that sets fake modes.
- **`pg_dsn` / `scratch_schema`**:
  - Skips without `REFMERGE_TEST_PG_DSN`, but *fails* when `REQUIRE_WAREHOUSE=1`, so CI can't skip silently.
  - Creates `refmerge_it_<uuid8>` per test and drops it with `cascade` in teardown through psycopg2 (installed with dbt-postgres). Teardown doesn't use the tool's own cleanup, which is under test.
- **`faults`**: fault injection with `sys.addaudithook`.
  - Audit hooks can't be removed, so install one dispatcher per session and register handlers inside a context manager.
  - Events that work on 3.11+: `open`, `os.chmod`, `os.rename` (raised by `os.replace`), `os.remove`, `fcntl.flock`.
  - This covers the race and `OSError` branches in §6.4 without mocks.

### 4.3 The fake dbt

`tests/fakes/fake_dbt.py` is invoked as `(sys.executable, path)` through `AppConfig.dbt_command` or `--dbt-command-part`. No PATH or shebang is needed, so it works on Windows. The review's prototype, about 50 lines, drove a full `check` in about 0.15 s.

| Invocation | Behavior |
|---|---|
| `--version` | dbt ≥ 1.5 format: `Core:` then `  - installed: 1.9.0` |
| `--help`, `compile --help` | a flag list, which drives capability discovery |
| `compile --target-path P [--select fqn:x]` | replaces `{{ ref('x') }}` with `"db"."sch"."x"` and writes a v12 `manifest.json` with `compiled_code`, `depends_on`, `refs`, `original_file_path`, `config.materialized` and `metadata.adapter_type` |

`FAKE_DBT_MODE` takes a comma-separated list of:
- `version_fail`, `compile_fail`, `candidate_compile_fail`
- `ignore_target_path`, `adapter_type=<name>`
- `candidate_drop_node`, `candidate_drift`
- `sleep=<seconds>`, `ignore_sigterm`, `sigint_parent`
- `big_logs`, `exit=<n>`

`FAKE_DBT_ARGV_LOG=<path>` appends each argv as a JSON line.

### 4.4 Production seams

Add a seam only where a test would otherwise be slow or impossible:

1. `DbtCli(..., terminate_grace_seconds=10.0)`, used at `dbt_cli.py:147`. The "child ignores SIGTERM" test drops from 11 s to about 1 s.
2. Rich consoles with `markup=False, highlight=False, soft_wrap=True` in `cli.py`. This fixes the markup bug and makes CLI output assertable.
3. Optional: a pure `compile_argv(...)` builder pulled out of `dbt_cli.py:229-258`, which turns about 14 argv branches into unit tests.
4. Optional, until a real runner exists: CLI commands take the service from `ctx.obj`. Then `CliRunner().invoke(app, args, obj=RefmergeService(verify_runner=...))` reaches the applied path at `cli.py:244-245`.

### 4.5 CI

Replace the `coverage: true` matrix flag with a dedicated job:

```yaml
  coverage:
    runs-on: ubuntu-latest
    permissions:
      contents: read
      id-token: write
    services:
      postgres:
        image: postgres:16
        env:
          POSTGRES_PASSWORD: postgres
          POSTGRES_DB: refmerge
        ports: ["5432:5432"]
        options: >-
          --health-cmd pg_isready --health-interval 5s --health-timeout 5s --health-retries 10
    env:
      REFMERGE_TEST_PG_DSN: postgresql://postgres:postgres@localhost:5432/refmerge
      REQUIRE_WAREHOUSE: "1"
      DO_NOT_TRACK: "1"
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install -e ".[dev,integration]"
      - run: pytest -q -m "" --cov --cov-report=xml
      - uses: codecov/codecov-action@v5
        with:
          files: coverage.xml
          use_oidc: true
          fail_ci_if_error: false
```

- The `gates` matrix runs `pytest -q -m "not warehouse"`: the unit and fake_dbt lanes on every OS, without `--cov`.
- `lowest-deps` stays as it is.
- Optional: a warehouse compatibility job on Python 3.11 with `dbt-postgres~=1.7` (manifest v11).
- Update README "Develop it" with the lane commands.

## 5. Phases

Each phase is one or more PRs. At the end of each, raise `fail_under` to the measured value.

| Phase | Scope | Exit criteria | Coverage (est.) | Size |
|---|---|---|---:|---|
| 0 | Infrastructure (§4); fix the leaking test | CI green, `fail_under = 60` | 60% | S |
| 1 | Decide the verifier (§5.1) | **Done 2026-09-16: build it (option A)** | — | — |
| 2 | Blocker and safety fixes, test-first (§2.1 B2–B7, §2.2) | each fix lands with the test that failed before it | ~65% | M |
| 3 | Unit-lane coverage and dead/defensive cleanup (§6.1–§6.4, §7) | all modules that don't shell out at 100% | ~85% | M |
| 4 | fake_dbt lane (§6.5) | `dbt_cli`, `orchestrator`, `cli`, `workspace` at 100% apart from warehouse-only paths | ~95% | M |
| 5 | Verifier and warehouse lane (§6.6) | `fail_under = 100` | 100% | L |

Phase 2 comes before 3 and 4 because tests written first would pin the bugs:
- Against B3 and B5, correct merges would be asserted as `COMPILE_DRIFT`.
- Against S1, `tablesample` would be asserted as deterministic.

Those tests would all have to be rewritten.

### 5.1 Decision: build the verifier (option A, chosen 2026-09-16)

About 10% of statements exist only for verification that nothing calls: `harness`, `comparator`, the unused `AdapterSpec` capability fields, `DbtCli.run` / `run_operation` / `parse`, and ledger recording.

**Option A: build it (recommended; it is the product).** Make it the default `verify_runner` whenever `spec.verifies`. It chains these steps:
1. `validate_scratch_schema`
2. `build_harness_project`
3. A target override so views land in `--scratch-schema`. Today `generate_schema_name` pins them to `target.schema`.
4. `dbt run` and `dbt parse`
5. `preflight_harness_manifest`
6. Schema-emit and compare macros. This means replacing the `...` placeholders, and emitting the marked JSON with `print()` because `log(info=True)` adds timestamps.
7. `parse_equality_result` / `derive_status`
8. `record_object`
9. Cleanup through `adapter.get_relation` and `drop_relation_if_exists`

S2–S6 are fixed as part of this work.

**Option B: remove it for 0.1 (not chosen).**
- Delete the verification modules and the unused `DbtCli` methods.
- Drop or disable `fix` and `cleanup`.
- Rewrite the README to describe `scan` plus the compiled-delta check.

100% arrives sooner, but the release does much less than the README describes today.

**Release gate:** don't tag or publish a release until Phase 5 is complete and the README's claims are covered by warehouse tests.

## 6. Test inventory by module

Test names are proposals. **U** = unit lane, **F** = fake_dbt lane, **W** = warehouse lane. Unmarked items are U.

### 6.1 Pure logic: reporting, config, errors, domain, analyze, rewrite

**`reporting.py`** → `tests/unit/test_reporting.py`
- `test_exit_code_policy_matrix`: every status (DIFFERENT, UNVERIFIABLE, ERROR, SNAPSHOT_EQUIVALENT, NOT_RUN) × every `FailOn`, plus empty and mixed result lists (36-78). Fix the `--fail-on` filtering first, then replace unreachable 51, 59-60 and 78 with a table lookup.
- `test_exit_code_never_is_ok`
- `test_check_report_json_counts_each_status` (90-138): five out-of-order results → exact `summary`, sorted models, relation dict keys, `schema_version == "1"`.
- `test_scan_report_json_shape` (155) and `test_render_human_check_includes_diff_only_when_present` (175-192).

**`config.py`** → `tests/unit/test_config.py`
- `test_load_config_reads_flat_toml`, `…_reads_tool_table`, `…_ignores_non_table_tool_entry` (98-107)
- `test_load_config_env_coercions`, `…_env_non_integer_timeout_rejected` (109-135, 53-55)
- `test_load_config_cli_overrides_win_and_none_ignored`, `…_toml_dbt_command_list_and_string` (136-142)
- `test_load_config_validates_profile_target_schema_adapter`, `…_requires_dbt_project_file`, `…_accepts_dbt_project_yaml`, `…_rejects_symlinked_project_dir` (146-162)
- `test_redact_mapping_masks_secret_names` (77-82), `test_check_external_string_rejects_control_and_length` (86-90)
- Delete `require_scratch_schema` (166-168, no callers) and the not-a-dict guard at 104→109 (`tomllib.load` always returns a dict).

**`errors.py`, `domain.py`** → `tests/unit/test_domain.py`
- `test_is_fixable_requires_every_equivalence_condition` (domain 196): start from one fixable receipt, then `dataclasses.replace` each of status, reason codes, cleanup, `schema_equal` and row counts → `False`.
- `SourceSpan(5, 1).validate(10)` raises `InternalInvariantError` (domain 73, errors 67). `ScratchBoundaryError` (errors 57) and `CleanupError` (errors 62) are covered by §6.6 and the orchestrator cleanup tests.
- `ConfigError` is never raised. Use it in `load_config` or delete it.

**`analyze.py`** (odd-scenario helpers)
- `test_group_imports_drops_single_member_upstream` (55→53)
- `test_projection_collision_reported_once` (80→76, 88→89)
- Delete the unused `star_blocked` / `comment_blocked` parameters (121, 123). Replace the dedupe loop with `tuple(reasons)` (128→127).

**`rewrite.py`**
- `test_validate_edits_rejects_overlap_and_out_of_bounds` (20)
- `test_build_plan_without_groups_refuses` (267)
- `test_whitespace_only_line_after_donor_is_collapsed` (236), byte-exact.
- `test_build_plan_refuses_collision_without_qualification` (138); `test_build_plan_refuses_ineligible_group` after S8.
- Convert 86, 90 and 218 to `assert`s: the first member is never a donor, and every non-last CTE has a separator. Delete 198 and 264 (both always set) and the 245→248 guard. Pragma the reparse guard at 279-283.

### 6.2 SQL front end: semantics, source

**`semantics.py`** → `tests/unit/test_semantics.py`; drift cases in `test_odd_scenarios.py`

| Test | Covers | Input → expected |
|---|---|---|
| `test_parse_model_refuses_unembeddable_compiled_sql` | 160-175 | `"  \n"`, `"select ("`, `"select 1; select 2"`, `"; -- c"`, `"insert into t values (1)"` → `INTERNAL_ERROR`; `"select {{ x }}"` → `HARNESS_EMBEDDING_UNSAFE` |
| `test_parse_model_without_with_has_no_ctes` | 182, 184→186 | `select 1` → `ctes == {}` |
| `test_qualify_import_cte_refuses_unsupported_shapes` | 276-349 | union, `group by`, `distinct`, `*`, `lower(id)`, `id + 1`, missing FROM → `UNSUPPORTED_IMPORT_SHAPE` with the expected `unexpected_nodes` |
| `test_predicate_fingerprint_non_select_is_none` | 404 | union CTE |
| `test_match_source_ctes_ignores_non_import_ctes` | 425 | unfiltered CTE list including `final` |
| `test_compiled_import_drift_refuses` | 429-497 | compiled SQL differs from source: renamed CTE, added `where`, altered projections (`stg.id as id`, `1 as id`, `idx as id`, `id as ident`) → `SOURCE_MAPPING_AMBIGUOUS`; added `group by` → `UNSUPPORTED_IMPORT_SHAPE` |
| `test_match_source_ctes_duplicate_semantic_match` | 517 | same CTE twice → `SOURCE_MAPPING_AMBIGUOUS` |
| `test_analyze_volatility_classifies_functions` | 533-591 | range predicate → ok; `my_udf(id)`, `row_number() over (order by id)` → `NONDETERMINISTIC` with names; the allowlist admits `lower`. After S1: `tablesample`, `fetch first`, `offset`, `distinct on`, unordered subquery LIMIT → `NONDETERMINISTIC`. Cases for `coalesce`/`lower`/`date_trunc` depend on whether `DETERMINISTIC_FUNCTIONS` gets wired in. |
| `test_expected_transform_refuses_inconsistent_baseline` | 608-644 | no WITH, unknown canonical, union canonical, addition `"from"` → `COMPILE_DRIFT` |
| `test_delta_gate_accepts_rewritten_candidate` | 412, 596-670 | terminal donor, `join b as x`, `join b x`, bare `join b` (B3), `amount as amt` (B5), donor named like a physical table (B6), two groups (B4) → accepted |
| `test_delta_gate_refuses_unchanged_candidate` | 608-670 | candidate == baseline → `COMPILE_DRIFT` |

Delete `_walk_types` (269, no callers), the duplicate table-count check at 444, the `comments`/`meta` skip at 375, and the `sql_name()` try/except at 542-543. Pragma the sqlglot API guards: 153-154, 177, 189, 192, 356.

**`source.py`** → `tests/unit/test_source_jinja.py`, `tests/unit/test_cte_splitter.py`

| Test | Covers | Inputs → expected |
|---|---|---|
| `test_mask_jinja_refuses_unterminated_or_undecodable` | 60-61, 235, 255, 296, 322 | `b"\xff"`, `{% raw %} x`, `{{ x`, `{% if`, `{# c` → `SourceParseError` |
| `test_non_literal_jinja_calls_are_not_sentinels` | 144-203 | `{{ ref('m' }}`, `{{ this }}`, `{{ foo.ref('m') }}`, `{{ ref(none) }}`, `{{ ref('m', version=var) }}`, `{{ ref(1) }}`, `{{ ref('') }}`, `{{ source('s') }}` → `ref_calls == {}` |
| `test_literal_ref_variants_capture_package_and_version` | 185, 189-190, 284→283 | `version=1`, `ref('p', 'm')`, a call spanning lines |
| `test_masking_preserves_newlines_in_every_jinja_kind` | 246→245, 289→288, 315→314, 320-335 | newline inside raw, `{{ }}`, `{% %}`, `{# #}` (no existing test uses a Jinja comment) |
| `test_source_without_with_has_no_ctes` | 455-469 | `select 1`, `values (1)` |
| `test_cte_list_syntax_refusals` | 483-545 | `recursive`, duplicate name, `with a x as`, `[not] materialized`, missing parens, unbalanced, trailing comma |
| `test_import_body_shapes` | 647-826 | about ten body shapes → `(ref_call is not None, projections)` |
| `test_downstream_ref_edge_shapes` | 881-1095 | table hints, quoted alias, qualified star with a join, Jinja relation, correlated subquery inside an import, comma join to a raw table |
| `test_scan_mixed_supported_and_unsupported_duplicate_imports` | 1133→1143, 1140→1141 | one `distinct` import → `NEEDS_COMPILED_ANALYSIS` / `UNSUPPORTED_IMPORT_SHAPE` |

Delete:
- `_strip_quotes_ident` (438-442) and `_split_depth_zero` (603-615)
- the inner `if` at 193
- the try/excepts at 149-150 and 212-213
- the `KeyError` guards at 529-530 and 553-554
- the non-`"` string branches at 765-766 and 781-782
- 1098, and merge the duplicate check at 1058 into 1050

Pragma 277-279: the sentinel only outgrows the shortest call after 10⁷ calls.

### 6.3 Artifacts and adapters

**`artifacts.py`** → new `tests/unit/test_artifacts.py`
- `test_load_manifest_nodes_models_and_get` (77, 80)
- `test_load_manifest_rejects` (87-130), each → `ArtifactError`: duplicate keys, invalid JSON, non-object root, bad metadata, node without `unique_id`, absolute / `..` / backslash-traversal paths, drive-letter path (S10), non-UTF-8.
- `test_load_manifest_rejects_oversized` (96), using a sparse `truncate(MAX_ARTIFACT_BYTES + 1)`.
- `test_resolve_ref_ambiguous_across_packages`, `…_package_arg_disambiguates`, `…_refs_metadata_disambiguates`, `…_refs_without_match_keeps_candidates` (149-169)
- `test_resolve_source_ambiguous_same_name_two_packages`, `…_owner_sources_without_match` (174-187)
- Delete 177-183: the loop at 172-176 already appended every matching dict source.

**`adapters.py`** → `tests/unit/test_adapters.py`
- `test_canonical_adapter_name_rejects` (110), `test_spec_for_dialect_unknown_fails_closed` (132)
- `test_project_profile_yaml_extension`, `…_absent_returns_none`, `…_unusable_returns_none` (147-162)
- `test_profiles_reader_yaml_extension`, `…_unusable_returns_none`, `…_defaults_to_home` (177-201)
- `test_resolve_adapter_without_profile_pointer_uses_manifest` (226→231)

### 6.4 Filesystem and atomic apply

**`workspace.py`** → new `tests/unit/test_workspace.py`
- `test_snapshot_copies_tree_prunes_runtime_dirs_and_hashes_stably` (85-145)
- `test_snapshot_rejects_symlinked_dir_outside_project`, `…_reads_through_in_project_file_symlink`, `…_rejects_file_symlink_outside_project`, `…_rejects_dangling_symlink` (91-112). Skip where `os.symlink` is unavailable.
- `test_snapshot_rejects_fifo` (113, POSIX only)
- `test_snapshot_detects_change_during_copy`, `…_delete_during_copy` (130-136), using the `faults` fixture on `open`.
- `test_package_state_digest_tracks_package_files_and_macros` (148-163)
- `test_cleanup_files_removes_root_unless_keep` (197-198). Once cleanup reports failures, add a read-only-dir test for 199-200.
- Delete `__enter__` / `__exit__` (unused) and the unreachable non-dir branches at 98-101. Keep `record_object` / `load_ledger` only under §5.1 option A, tested by `test_record_object_upserts_by_relation`.

**`apply_verified_source`** (`orchestrator.py:593-658`) → new `tests/unit/test_apply_source.py`
- `test_apply_refuses_non_regular_target`, `…_refuses_candidate_digest_mismatch` (603-606)
- `test_apply_refuses_concurrent_edit_during_temp_write` (630-634): a `faults` handler on `os.chmod` rewrites the target.
- `test_apply_replace_failure_keeps_original` (648-650): `os.rename` raises.
- `test_apply_lock_failure_fails_closed` (614-615) once S9 is fixed. Until then, `test_apply_tolerates_flock_and_dir_fsync_errors` (614-615, 643-644, 657-658).
- `test_apply_leaves_no_lock_file` after S9.
- Platform pragmas on 609, 636, 652.

### 6.5 dbt subprocess, orchestration, CLI

**`dbt_cli.py`** → `tests/fake_dbt/test_dbt_cli.py` (**F** unless marked U)

| Test | Covers |
|---|---|
| U `test_dbt_cli_rejects_empty_command` | 101-104; errors 17, 31-32 |
| U `test_launch_failure_is_dbt_error` (missing executable, no child) | 121-139 |
| U `test_redact_argv_masks_secret_values` (after S11) | 62-74 |
| `test_version_parses_dbt_core_output` (B2), `…_nonzero_exit_raises`, `…_without_core_line_uses_first_line`, `…_empty_output` | 175-185 |
| `test_discover_capabilities_from_compile_help` | 188-205 |
| `test_compile_argv_all_options`, `…_minimal` (through `FAKE_DBT_ARGV_LOG`) | 229-258 |
| `test_compile_passes_env_overrides` | 124→125 |
| `test_compile_timeout_raises_dbt_error` (`sleep`, 1 s timeout) | 143-147 |
| `test_timeout_kills_group_ignoring_sigterm` (POSIX; needs seam 4.4.1) | 148-150 |
| `test_keyboard_interrupt_terminates_child` (POSIX; fix the unreaped child first) | 151-153 |
| `test_run_argv_caps_logs`, including non-ASCII | 156-159 |
| `test_terminate_and_kill_tolerate_reaped_process` | 79-96 (win32 arms by pragma) |

Delete `from_config`, `capabilities` and `shlex_join` (108, 112, 316). Keep or delete `parse` (208-226), `run` (261-281) and `run_operation` (289-312) according to §5.1.

**`orchestrator.py`**

U → `tests/unit/test_orchestrator.py`:
- `test_discover_model_files_prefers_models_dir`, `…_falls_back_to_tree_skipping_target_and_packages` (110-118; switch the filter to project-relative paths first)
- Six `test_detect_*` tests for `detect_source_duplicates` (130-185): unparseable source, single import, distinct refs, earliest group, and upstream resolution (no manifest / owner missing / resolved / ambiguous)
- `test_scan_without_manifest_uses_stem_uid`, `…_with_manifest_maps_uid_and_upstream`, `…_ignores_invalid_manifest` (199-230)
- `test_check_requires_scratch_schema` (236)
- `_check_one_model` early exits (319-371), called directly with `DbtCli(("never-invoked",))`: unsupported materialization, source-path fallbacks, backtick duplicates, semantic mismatch, no duplicates, ineligible group, plan refusal
- `test_validate_delta_*` (418-435), sharing cases with the semantics delta-gate test
- `test_selected_filters` (499-506), `test_unified_diff_headers_and_non_utf8` (510-515), three `test_cleanup_*` tests (471-477), and an `adapter_type=None` case in `test_require_manifest_adapter_agreement` (484)

F → `tests/fake_dbt/test_check_fix.py`:
- **`check`** (251-409):
  - `test_check_version_failure_raises_and_removes_workspace`
  - `…_baseline_compile_failure`
  - `…_uses_project_target_manifest_when_target_path_ignored`
  - `…_manifest_adapter_mismatch`
  - `…_no_selected_model_raises`
  - `…_keep_workspace_retains_directory`
  - `…_candidate_compile_failure`
  - `…_candidate_node_missing_is_compile_drift`
  - `…_candidate_drift_is_compile_drift`
  - `…_without_verifier_reports_input_isolation_unavailable` (replaced once §5.1 lands)
  - `…_with_verifier_returns_verifier_receipt`
  - `test_check_continues_past_unparseable_model` (B7)
- **`fix`** (443-467):
  - `test_fix_unknown_path_is_model_not_found`
  - `…_unverified_model_is_not_fixable`
  - `…_dry_run_leaves_file_untouched`
  - `…_applies_verified_candidate`
- Delete the try/except that only re-raises (257-258) and `_is_secret` (494-495, a duplicate of `config.is_secret_name`).

**`cli.py`** → `tests/unit/test_cli.py` (U, `CliRunner`) and `tests/fake_dbt/test_cli.py` (F)
- **U, `scan`:**
  - `test_cli_scan_human_lists_duplicate_ctes`
  - `…_scan_json_shape`
  - `…_scan_no_findings_prints_nothing`
  - `…_scan_missing_dbt_project_config_error`
  - `…_scan_unresolvable_adapter_exit_1`
- **U, `check`, `fix` and `cleanup`:**
  - `test_cli_check_missing_dbt_project_config_error`, `…_check_without_scratch_schema_fails`, `…_check_all_options_unsupported_adapter`
  - `test_cli_fix_*` config and service errors
  - `test_cli_cleanup_reports_run_id`, `…_without_dbt_project_falls_back`
  - `test_cli_human_output_preserves_brackets_in_paths` (after seam 4.4.2)
- **F:**
  - `test_cli_check_json_with_fake_dbt`
  - `…_check_human_exit_code_unverifiable`
  - `test_cli_fix_model_not_found_exit_1`
  - `…_fix_not_fixable_json`, `…_fix_not_fixable_human`
  - `…_fix_applies_verified_merge` (needs seam 4.4.4 or the W lane)
- `if __name__ == "__main__"` (267-268) is excluded by covdefaults.

### 6.6 Verification and warehouse

**Pure parts** (U) → `tests/unit/test_harness_comparator.py`, needed if §5.1 keeps the modules.
- **harness:**
  - `test_validate_scratch_schema_folds_unquoted_preserves_quoted`
  - `…_rejects` (27-44): forbidden schema, same as the model schema, NUL, length, malformed quotes (S5), over 63 bytes (S4)
  - `test_strip_semicolon_*` (96, 101)
  - `test_build_harness_project_writes_expected_tree`, `…_rejects_endraw_variants` (S3) (117-136)
  - `test_preflight_accepts_harness_nodes` and `test_preflight_rejects` (148-176): materialization, each of the four hook keys, schema/database outside scratch, alias, extra writable node
  - `test_build_verdict_sql_dispatches` (180-190)
  - `test_ledger_relations_round_trip` (194-204)
- **comparator:**
  - `test_normalize_schema_normalizes_types`, `…_orders_by_ordinal`
  - `test_schemas_equal_ignores_payload_order_and_aliases`, `…_detects`
  - `test_validate_types_accepts_parameterized`, `…_rejects` (float, jsonb, arrays, user-defined)
  - `test_grouped_counts_marker_avoids_collision`
  - `test_parse_marked_json_rejects` (missing or duplicate markers, wrong nonce, multiple lines, over 1 MB, bad JSON)
  - `test_parse_equality_result_rejects`, including `"false"` for `schema_equal` (S2)
  - `test_derive_status_different`

**W lane.** Counts are `(baseline_rows, candidate_rows, baseline_only, candidate_only)`.

| Test | Fixture → expected |
|---|---|
| `tests/warehouse/test_comparator_sql_pg.py::test_verdict_counts`, parametrized over both strategies | same rows in a different order → equal<br>`{1,1,2}` vs `{1,2,2}` → (3,3,1,1)<br>`{1,1}` vs `{1}` → (2,1,1,0)<br>identical NULL-bearing rows → equal<br>`{NULL,NULL}` vs `{NULL}` → baseline-only 1<br>`(1,NULL)` vs `(1,0)` → different<br>empty vs empty → zeros<br>empty vs two rows → (0,2,0,2)<br>`'A'` vs `'a'` and `'a'` vs `'a '` → different<br>`1.0` vs `1.00` → equal (pin and document)<br>one instant in two UTC offsets (timestamptz) → equal<br>one-bit bytea / uuid / bool change → different<br>column names `"Order ID"`, `a"b`, `__dbt_refmerge_side`, `_a` → handled (the grouped strategy currently errors on `_a`, which fails safe) |
| `…::test_verdict_matches_counter_property` | Hypothesis bags of (int or NULL, text or NULL) → matches `collections.Counter` arithmetic |
| `tests/warehouse/test_harness_dbt_pg.py::test_harness_run_creates_two_views_in_scratch_schema` | exactly two views in `information_schema.views`, both in the scratch schema |
| `…::test_harness_verdict_over_real_views` | baseline `values (1),(1)` vs candidate `values (1)` → (2,1,1,0) |
| `…::test_harness_cleanup_drops_views` | views gone, ledger updated |
| `tests/warehouse/test_end_to_end_pg.py::test_check_proves_disjoint_projection_merge` | golden `disjoint_projections` project → `SNAPSHOT_EQUIVALENT` |
| `…::test_check_rejects_divergent_candidate` | a candidate whose rows differ → refused before comparison, or `DIFFERENT` |
| `…::test_fix_applies_and_cleanup_drops_scratch` | file bytes equal the golden `expected.sql`; scratch schema empty after `cleanup` |

## 7. Pragma budget and conditional code

**`# pragma: no cover` in use at 100%** (each carries a comment naming the invariant):
- `semantics.py`:
  - the non-`Identifier` CTE alias fallback (sqlglot API guard)
  - a CTE without an alias
  - a nested `Semicolon` node
  - an import CTE reading other than one table
- `source.py`: the sentinel-wider-than-call guard (needs 10**7 calls in one file).
- `rewrite.py`:
  - the terminal-donor and donor separator refusals (typed refusals that rely on `parse_source_model`, never asserts)
  - the post-rewrite reparse guard

**Platform and version pragmas** (covdefaults, still executed by the matching CI job):
- `dbt_cli.py`: `_terminate` / `_kill` win32 and POSIX arms.
- `orchestrator.py`: four `sys.platform` checks in `apply_verified_source`.
- `workspace.py`: the `rmtree` `onexc` (3.12+) and `onerror` (3.11) arms.

**Depends on §5.1:** kept and tested under option A, deleted under option B.
- `verification/harness.py`, `verification/comparator.py`
- unused `AdapterSpec` capability fields
- the `Comparator` protocol
- `DbtCli.run` / `run_operation` / `parse`
- `RunWorkspace.record_object` / `load_ledger` and `ScratchObject`
- the duplicate `COMPARATOR_VERSION` (`semantics.py:23`, `orchestrator.py:543`)

## 8. Beyond the number

- **Coverage shows code ran, not that a regression would be caught.** For the safety-critical modules (`semantics`, `rewrite`, `source`, `verification/*`), run mutation testing (for example `mutmut`) before each release. Treat surviving mutants in refusal paths as missing tests.
- **Extend the property tests.** Keep the comparator's `Counter` property test. Extend `tests/property` to the rewrite: for generated disjoint projections, the candidate reparses and the delta gate accepts it. That property would have caught B3.
- **Every fix in §2 lands with the test that failed before it.** Most of the new coverage should arrive that way.
