# dbt-refmerge — Revised Architecture (muse.md)

> Review of `Finalized Architecture` (2026-09-15). Verdict: direction is correct,
> scope is correctly narrow, verification-first invariant is the right differentiator.
> Changes below tighten safety, simplify v1, and fix 5 load-bearing gaps:
> source-mapping, isolated execution, same-input guarantee, multiset equality,
> and prod-safety.

---

## 0. Verdict

Keep:

- Standalone CLI outside dbt. Correct boundary.
- `STATICALLY ELIGIBLE → EXECUTE BOTH → COMPARE → APPLY ONLY IF EQUAL`. Correct invariant.
- Compiled SQL for semantics, source SQL for edits, manifest as bridge. Correct split.
- Conservative v1 (direct projections, exact-predicate match, prefer false negatives). Correct risk posture.

Change (load-bearing):

1. **Source mapping strategy is unsound as drawn.** Compiled-SQL AST spans do not map
   to source/Jinja spans. Must invert: parse source with Jinja-masking to get editable
   spans, use compiled SQL only for semantic labels joined by CTE name + ordinal.
2. **Candidate workspace as "copy project" is under-specified and will break**
   on packages, profiles, seeds, and `generate_schema_name`. Specify exact isolation
   mechanism (shadow models + isolated schema + alias override, not full project fork).
3. **Same-input guarantee is hand-waved.** Sequential runs without input fingerprinting
   will produce false FAILs (and worse: false PASS on nondeterministic models).
   v1 needs explicit input-fingerprint + nondeterminism gate, warehouse time-travel only as opt-in.
4. **Table equality as described will false-PASS.** `EXCEPT` (distinct) is bag-unsound;
   `GROUP BY all columns` breaks on NULL semantics, floats, and struct types.
   Specify exact comparator SQL per warehouse capability (`EXCEPT ALL` vs grouped-count).
5. **No prod-safety / cost story.** A tool that auto-runs models must hard-block prod
   targets, force temp schemas, enforce cleanup-always, and cap bytes scanned. Missing now.

Simplify:

6. Package layout proposes ~30 modules before any code exists. Collapse to ~10 files for v1.
   Split only when a file exceeds ~500 lines or a second transform needs it.
7. `VerificationAdapter` interface as drawn couples execution + comparison + cleanup per
   warehouse. Split into `Executor` (dbt, warehouse-agnostic) + `Comparator` (small SQL
   dialect switch) + `InputGuard` (warehouse-specific). Only `Comparator`/`InputGuard`
   vary by warehouse in v1.

---

## 1. Answers to the 20 architecture questions

1. **Standalone CLI? Yes.** Keeps install (`uv tool install`), versioning, and warehouse
   auth independent of the user's dbt version. Do not make it a dbt package — that would
   couple release cadence and pollute `packages.yml`.
2. **CLI + artifacts over dbt-core imports? Yes for v1.** `subprocess: dbt compile/run`
   + `manifest.json` is the only stable cross-version boundary. Pin support matrix
   (dbt-core 1.5–1.9) and detect version via `dbt --version`. Revisit programmatic import
   only for performance (manifest-in-memory) in v2.
3. **Compiled vs source split? Yes, but join key must be (model, CTE name, ordinal),
   not byte offsets.** Compiled SQL never round-trips to source. Source is the only
   thing edited; compiled is the only thing trusted for semantics.
4. **Safest source-mapping? Jinja-masked source parse, not compiled→source back-mapping.**
   Procedure: mask `{{...}}`/`{%...%}` to placeholders, split top-level `WITH` CTEs by
   balanced-paren tokenizer, record `(name, source_span)`. Separately parse compiled SQL
   with sqlglot for semantics. Join on CTE name (case-insensitive, ordinal-disambiguated).
   Any join failure → `AMBIGUOUS`, no rewrite. Never use compiled line numbers.
5. **sqlglot sufficient? Yes for v1 with strict bail-out.** Use per-adapter dialect
   (`snowflake`, `bigquery`, `databricks`→`spark`, `redshift`, `postgres`). Any parse
   error, unparsed macro residue, or unsupported node type → `UNSUPPORTED`. Do not attempt
   to support semi-structured (`FLATTEN`, `UNNEST`), `PIVOT`, `QUALIFY`, `MATCH_RECOGNIZE`
   in v1.
6. **Lineage: internal minimal, not a library.** v1 needs only *direct-column lineage*:
   `select a, b AS c` where every output expr is a bare column ref (optionally aliased).
   ~100 lines on top of sqlglot. Do not adopt `sqllineage`/OpenLineage yet — heavier dep,
   wider semantics than v1 allows, harder to audit for safety.
7. **Exact-AST predicate equivalence? Yes, appropriately conservative.**
   Normalize (lowercase keywords/identifiers per dialect case rules, strip parens/whitespace,
   sort commutative `AND` operands canonically? No — keep order-sensitive in v1, accept
   false negatives). Hash normalized AST. Anything short of byte-identical normalized
   hash → `INTENTIONAL_SLICE` or `AMBIGUOUS`. Symbolic equivalence is explicitly non-goal v1.
8. **Import-CTE definition narrow enough? Almost — convert denylist to allowlist.**
   Current text lists rejects but leaves holes (`ORDER BY`, `QUALIFY`, `FETCH`, scalar
   UDFs, implicit casts). v1 allowlist: single `FROM {{ ref/source }}`, single query block,
   output exprs ∈ {bare column, `alias.*` only if statically expandable else reject},
   optional single `WHERE` with deterministic operators only, no other clauses except
   trivial `ORDER BY` inside CTE (ignored semantically — actually forbid to stay simple).
   Everything else → `UNSUPPORTED`.
9. **Temp candidate workspace? Shadow-model overlay, not project copy.**
   Full project copy breaks `packages/`, `seeds`, `snapshots`, relative `source_paths`,
   and custom `generate_schema_name`. v1: write `<model>__refmerge_candidate` + 
   `<model>__refmerge_baseline` shadow `.sql` files into the *same* project temp dir
   (or `--target-path` overlay), each with `{{ config(alias=..., schema=tmp_schema,
   materialized='table') }}`. Run `dbt run --select` on those two nodes only. Delete
   after. No working-tree mutation: stage shadows in `target/dbt-refmerge/` and point
   dbt at them via `--project-dir` overlay only if adapter supports it; simplest correct
   v1 is git-ignored `.dbt-refmerge/shadows/` inside project + `dbt run --select`.
10. **Baseline without overwriting? Alias + schema override, materialized=table.**
    Never `dbt run` the real model name during verification. Baseline shadow pins
    `materialized='table'`, `schema='<tmp>_baseline'`, `alias='...'` so the real relation
    (including incremental state) is untouched. Also force `--full-refresh` semantics by
    construction (shadow is a new table node, no incremental logic inherited — copy only
    the compiled SELECT body, not the materialization config).
11. **Candidate under different name? Same mechanism.** Candidate shadow = patched source
    body + same forced config with different alias. Both compile through the user's real
    adapter/macros so Jinja/macros behave identically. Compile both first (`dbt compile
    --select shadows`) and diff the *compiled* candidate vs baseline to confirm only the
    intended CTE change differs — cheap pre-execution sanity gate.
12. **Same logical input? Fingerprint + gate, time-travel opt-in.**
    v1 default: (a) static nondeterminism scan rejects `current_timestamp/random/uuid/...`;
    (b) capture input relation fingerprints (`max(updated_at)` if available else
    `count(*)` + warehouse `last_altered`) immediately before each run; abort with
    `INPUT_CHANGED` if fingerprints differ between baseline and candidate runs;
    (c) run back-to-back, document residual race. Time-travel/clone (`AT TIMESTAMP`,
    zero-copy clone, BigQuery snapshot decorators, transaction `SERIALIZABLE`) behind
    `--isolation=time-travel|clone|none` per warehouse in v2. Never silently claim
    equivalence across divergent inputs.
13. **Per-warehouse differences? Isolate to two small switches** (see §5 table).
    Execution stays `dbt run` everywhere. Only comparator SQL (`EXCEPT ALL` support:
    Postgres ✓, Snowflake ✓, BigQuery ✓ (`EXCEPT DISTINCT` vs `EXCEPT ALL` gotcha),
    Redshift ✗, Databricks ✓) and input-guard (time-travel syntax) vary. Everything else
    is warehouse-agnostic.
14. **Multiset equality, warehouse-independent? Two-tier with explicit NULL/float rules.**
    Tier 1 (cheap): schema + `COUNT(*)`. Tier 2 (exact): bag difference both directions
    with `EXCEPT ALL` where available; fallback `GROUP BY` + `FULL OUTER JOIN` on
    grouped counts. NULLs: `IS NOT DISTINCT FROM` semantics (NULL = NULL for equality).
    Floats: exact bitwise by default; opt-in `--float-tolerance` uses `ABS(a-b) <= eps`
    only on declared float columns. Ordering: ignored (relations are multisets); if model
    has `ORDER BY ... LIMIT`, verification must include the `LIMIT` deterministically —
    flag `ORDER BY` without unique key + `LIMIT` as `UNSUPPORTED`.
15. **Reuse audit-helper/dbt_utils? No for comparator; yes for inspiration.**
    `dbt-audit-helper.compare_relations` and `dbt_utils.equality` are set-based or
    column-subset tools, not strict bag-equality, and require audit scaffolding. Implement
    ~80-line comparator generating explicit SQL (auditable, no extra dep). Reuse their
    column-name normalization ideas.
16. **Incremental / ephemeral in v1? Exclude ephemeral; incremental as isolated full table.**
    Ephemeral: cannot materialize standalone → `UNSUPPORTED` with message "verify via
    downstream materialized model" (future: inline into downstream shadow). Incremental:
    shadow copies SELECT body with `materialized='table'`, drops `is_incremental()` branches
    by compiling with `full_refresh=true` var — then compares full outputs. Document that
    this validates logic, not incremental merge behavior.
17. **Source-preserving rewrite practical? Yes if sqlglot never prints source.**
    Rule: sqlglot decides *what* changes; string-span surgery decides *how bytes change*.
    Steps: (a) keep original file text; (b) splice canonical CTE's select-list span to add
    missing columns (copy exact source text of those column exprs from donor CTEs);
    (c) delete donor CTE spans (including trailing comma handling); (d) regex-token rename
    of `donor_alias.` → `canonical.` outside string literals/comments. Verify by re-parsing
    patched source (masked) + `git diff --stat`. Any Jinja inside select list that sqlglot
    cannot classify → `AMBIGUOUS`. No templating-aware parser needed for v1.
18. **False-equality hazards? Yes — five must-gates:** (a) nondeterministic functions;
    (b) sampling/limit truncation (require full `COUNT(*)` comparison, forbid `--limit`
    during verification); (c) type-coercion masking (`varchar '1'` vs `int 1` — require
    type-compatible schema check, not just name check); (d) case/collation differences
    (normalize per warehouse collation, Snowflake upper-case default); (e) truncated
    comparison (cap rows → must compare all rows; if table > `--max-verify-rows`, abort
    with `TOO_LARGE`, don't sample). All five are hard FAILs, not warnings.
19. **Security/safety? Three hard rules:** (a) refuse `--target prod` (or any target whose
    `schema`/`database` matches prod allowlist or lacks `refmerge` prefix) unless
    `--allow-prod` + interactive confirm; (b) least-privilege temp schema
    (`dbt_refmerge_tmp_*`, auto-cleanup in `finally` + orphan `dbt-refmerge cleanup`
    command); (c) identifier-quoting everywhere (never interpolate model/CTE names into
    SQL via f-string — use adapter-quoted identifiers), plus warehouse cost guard
    (`--max-bytes` for BigQuery, statement timeout, row-cap abort).
20. **Generalizes without overengineering? Yes if v1 extracts one interface now:**
    `Transform = {detect, qualify, plan, patch}` + shared `verify(baseline, candidate)`
    pipeline. Duplicate-merge is the only `Transform` in v1. Do not build plugin registries,
    generic rewrite DSLs, or per-warehouse class hierarchies until transform #2 exists.

---

## 2. Critical fixes (do these before code)

### F1. Join semantics: source spans × compiled labels (not compiled spans)

```
source.sql --mask Jinja--> CTE splitter --> [(name, span, select_list_span)]
compiled.sql --sqlglot-->  [(name, upstream_node, predicate_hash, projections)]
                                            JOIN on (lower(name), ordinal)
```

Mismatch/duplicate-name collision → `AMBIGUOUS`.

### F2. Shadow-model isolation (replace §18–19 "project copy")

- Shadows live at `.dbt-refmerge/shadows/<run_id>/baseline_<model>.sql`,
  `candidate_<model>.sql`, each headed with forced config.
- `dbt run --select shadow_nodes --target <user-target>` writes only to
  `dbt_refmerge_tmp_<run_id>` schema.
- `finally: dbt run-operation refmerge_cleanup` + `DROP SCHEMA ... CASCADE`.
- Add `dbt-refmerge cleanup --older-than 24h` for orphans.

### F3. Nondeterminism + input-fingerprint gate (replace §20 fallback)

Pre-run scan (sqlglot function allowlist): any of
`current_timestamp|now|current_date|rand|random|uuid|uniform|generate_uuid|...`
→ `UNVERIFIABLE`, report only. Fingerprint inputs via manifest `depends_on` +
`SELECT COUNT(*)` per input (cheap, dialect-free) before each run; abort on drift.

### F4. Bag-equality spec (replace §22)

```
equal := schemas_compatible AND count_equal AND bag_diff_empty_both_ways
schemas_compatible := same ordered (name, normalized_type) after
  warehouse case-fold (Snowflake UPPER, others lower) AND no extra/missing cols
bag_diff := EXCEPT ALL both directions (or grouped-count fallback)
```

Float columns: exact unless `--float-tolerance` given. Never `EXCEPT DISTINCT` alone.

### F5. Prod/cost guardrails (new section, was missing)

- Default-deny `prod` targets; temp-schema prefix enforced.
- `--max-verify-rows` (default e.g. 50M, abort above), BigQuery `--max-bytes`.
- `check` never writes source; `fix` only writes `VERIFIED SAFE` + clean `git status`
  (refuse if working tree dirty unless `--force-dirty`).

---

## 3. High-value simplifications

- **Collapse package to v1-minimal (~10 modules):**
  `cli.py`, `project.py` (loader+manifest), `cte_split.py` (source spans),
  `semantics.py` (sqlglot import/predicate/projection), `classify.py`,
  `plan.py`, `patch.py` (span surgery), `shadows.py` (baseline/candidate build+run),
  `compare.py`, `report.py`. Split `adapters/` only into `compare_sql.py`
  (5 dialect functions) + `input_guard.py`. Delete `domain/` — use 4 dataclasses in
  `models.py`. Delete `dbt/environment.py`, `compile.py`, `run.py` — one `dbt.py`
  subprocess wrapper.
- **Canonical CTE choice deterministic:** fewest-required-edits, then lexicographically
  smallest name, then first ordinal. No heuristics, no LLM.
- **Pre-execution compiled-diff gate:** if compiled baseline vs compiled candidate differ
  outside the planned CTE region, abort (catches macro/Jinja drift).
- **DuckDB-first tests:** run integration fixtures on DuckDB (zero-infra CI) + one
  dockerized Postgres; Snowflake/BigQuery only via recorded compiled SQL + comparator
  unit tests in v1.

---

## 4. Revised v1 allowlist (import CTE)

A CTE qualifies iff ALL hold (else `UNSUPPORTED`/`AMBIGUOUS`, never merge):

- Single `SELECT` (no `UNION/INTERSECT/EXCEPT`), single `FROM` (one `ref`/`source`),
  no `JOIN`, no `GROUP BY/HAVING`, no `DISTINCT`, no `WINDOW`, no `LIMIT/OFFSET/FETCH`,
  no `QUALIFY/PIVOT/UNPIVOT`, no `ORDER BY` (v1 strict).
- Select items each ∈ {bare column ref, `col AS alias`}; no exprs, casts, function calls,
  literals, `*` (unless statically expandable via catalog — default reject).
- Optional `WHERE`: only deterministic comparison/`AND/OR/NOT`/`IS NULL`/IN-list over
  bare columns + literals; normalized-AST hash must match across group exactly.
- No Jinja control flow (`{% if/for %}`) inside the CTE body that affects shape;
  plain `{{ ref/source }}` and `{{ config }}`-free body only. Any other `{{...}}` → `AMBIGUOUS`.
- Upstream resolves to exactly one manifest `unique_id` for all group members.

---

## 5. Warehouse matrix (v1: only two cells vary)

| Capability | Snowflake | BigQuery | Databricks | Redshift | Postgres/DuckDB |
|---|---|---|---|---|---|
| Execute | `dbt run` (same) | same | same | same | same |
| Bag diff | `EXCEPT ALL` ✓ | `EXCEPT ALL` ✓ (watch `DISTINCT` default) | `EXCEPT ALL` ✓ | ✗ → grouped-count fallback | `EXCEPT ALL` ✓ |
| NULL equality | `EQUAL_NULL` / `IS NOT DISTINCT FROM` | `IS NOT DISTINCT FROM` | `<=>` / `IS NOT DISTINCT FROM` | `NVL`-trap: use grouped fallback with `IS NOT DISTINCT FROM`-equivalent | `IS NOT DISTINCT FROM` |
| Input fingerprint | `COUNT(*)` + `LAST_ALTERED` | `COUNT(*)` + `last_modified_time` | `COUNT(*)` + `DESCRIBE HISTORY LIMIT 1` | `COUNT(*)` only | `COUNT(*)` only |
| Time-travel (v2 opt-in) | `AT(TIMESTAMP=>...)` | `FOR SYSTEM_TIME AS OF` | `@vN` / time travel | unsupported | unsupported |

Default v1: `COUNT(*)` fingerprint + back-to-back runs everywhere. No warehouse-specific
executor classes.

---

## 6. Revised package layout (v1)

```
src/dbt_refmerge/
  cli.py          # Typer: scan/check/fix/cleanup; --json/--dry-run/exit codes
  dbt.py          # subprocess wrapper: version/compile/run/run-operation; target guard
  project.py      # loader + manifest + model discovery + dialect select
  cte_split.py    # Jinja-masked CTE span splitter (source truth)
  semantics.py    # sqlglot: import shape, predicate hash, projection safety
  classify.py     # STATICALLY_EQUIVALENT | INTENTIONAL_SLICE | UNSUPPORTED | AMBIGUOUS | UNVERIFIABLE
  plan.py         # MergePlan incl. canonical choice + required columns
  patch.py        # span surgery + reference rename + compiled-diff gate
  shadows.py      # shadow model build, isolated run, fingerprint gate, cleanup-always
  compare.py      # schema + count + bag-diff SQL generation + float policy
  models.py       # ImportCTE, DuplicateGroup, MergePlan, VerificationResult
  report.py       # Rich console + --json + diff + exit codes
tests/
  unit/           # splitter, predicate hash, classifier, patch spans, comparator SQL
  golden/         # input.sql -> expected.sql (minimal diff, Jinja/comments preserved)
  integration/    # DuckDB + Postgres fixtures: identical/filtered/slice/star/alias/
                  #   incremental/nondeterministic/ephemeral-excluded
  fixtures/dbt_projects/...
```

Delete for v1: per-warehouse adapter classes, `domain/`, `lineage.py` subsystem,
`orchestrator/baseline/candidate` split (one `shadows.py`), `artifacts.py`/`manifest.py`/
`model.py` split (one `project.py`).

---

## 7. Revised domain model (add spans + hashes + guards)

```python
@dataclass(frozen=True)
class ImportCTE:
    name: str
    ordinal: int
    model_id: str
    upstream_node_id: str          # manifest unique_id, not relation string
    predicate_hash: str | None     # normalized AST hash; None = no WHERE
    projected_columns: tuple[str, ...]   # output names, ordered
    source_span: tuple[int, int]   # byte offsets in ORIGINAL source file
    select_list_span: tuple[int, int]
    dialect: str

@dataclass(frozen=True)
class DuplicateGroup:
    model_id: str
    upstream_node_id: str
    imports: tuple[ImportCTE, ...]
    classification: str            # + reason code, never bare string

@dataclass(frozen=True)
class MergePlan:
    model: str
    upstream_node: str
    canonical_cte: str
    removed_ctes: tuple[str, ...]
    required_columns: tuple[str, ...]      # ordered, de-duplicated
    reference_rewrites: dict[str, str]
    candidate_source: str                  # full patched text (in-memory)
    compiled_diff_summary: str | None = None

@dataclass(frozen=True)
class VerificationResult:
    model: str
    baseline_relation: str
    candidate_relation: str
    input_fingerprint_before: str
    input_fingerprint_after: str
    schemas_equal: bool
    row_counts_equal: bool
    contents_equal: bool
    baseline_rows: int
    candidate_rows: int
    difference_count: int
    verified: bool
    reason_code: str              # e.g. OK | SCHEMA_MISMATCH | BAG_DIFF | INPUT_CHANGED |
                                  #   UNVERIFIABLE_NONDETERMINISM | TOO_LARGE | COMPILE_DRIFT
```

---

## 8. Verification SQL (normative, replaces §22 prose)

```sql
-- Tier 1: counts (all warehouses)
SELECT COUNT(*) FROM <baseline>;
SELECT COUNT(*) FROM <candidate>;
-- Tier 2a: bag diff where EXCEPT ALL exists (PG/Snowflake/BQ/Databricks)
(SELECT * FROM <baseline> EXCEPT ALL SELECT * FROM <candidate>)
UNION ALL
(SELECT * FROM <candidate> EXCEPT ALL SELECT * FROM <baseline>)
LIMIT 101;  -- 0 rows => equal; >0 => FAILED with sample (cap sample, count separately)
-- Tier 2b fallback (Redshift): grouped-count FULL OUTER JOIN on all cols with
-- IS NOT DISTINCT FROM-equivalent null-safe join, compare cnt deltas; sum(abs(delta)) = 0 => equal.
-- Schema: compare ordered (lower(name), normalized_type) after warehouse case-fold;
-- type map: varchar/text/string, int/bigint, numeric/decimal, float/double,
--   timestamp(+tz), date, bool, struct/array => require exact match or UNVERIFIABLE.
```

Rules: compare ALL rows (no sampling for verdict; `LIMIT 101` is only the diagnostic
sample — verdict query is `SELECT COUNT(*) FROM (diff)`); floats exact by default;
collation/case per warehouse documented in `compare.py` header.

---

## 9. CLI contract (tighten §26)

- `scan` — no warehouse I/O. Exit 0 clean / 2 findings / 1 error. `--json` stable schema.
- `check` — scan + shadows + compare. Never writes source. `--only <model>`,
  `--isolation none|fingerprint` (default fingerprint), `--max-verify-rows`,
  `--float-tolerance`, `--json`. Dirty tree allowed (no writes).
- `fix` — only `VERIFIED SAFE` + `reason_code==OK`; refuses dirty tree unless
  `--force-dirty`; writes minimal patch; re-runs `dbt compile` on patched model;
  prints `git diff`. `--dry-run` = print diff, write nothing. `--apply-all` requires
  explicit flag; default is per-file confirm.
- `cleanup` — new: drops orphan `dbt_refmerge_tmp_*` schemas/relations older than N hours.
- Prod guard global: `--target` inspected; `prod` (or schema without tmp prefix)
  → hard error unless `--allow-prod`.

---

## 10. Build order (adjust §33)

1. `scan` end-to-end on fixtures (splitter + semantics + classify + report).
2. `patch` + golden tests (minimal diff, Jinja/comments preserved).
3. `shadows` + `compare` on DuckDB (first real `check`).
4. `fix` gated on verification + prod/cost guards.
5. CI/JSON/changed-only (`--select state:modified+`), Postgres parity, then add
   warehouse comparator cells one at a time. Time-travel isolation last (v2).

Non-goals v1: symbolic predicate equivalence, `SELECT *` expansion, ephemeral support,
incremental-merge validation, multi-model/global rewrites, LLM assistance, plugin API.

---

## 11. Top risks if shipped as-is (original arch)

| # | Risk | Effect |
|---|---|---|
| 1 | Compiled→source offset mapping assumed | Wrong edits, destroyed Jinja, silent corruption |
| 2 | Project-copy workspace | Broken packages/seeds/profiles, slow, flaky CI |
| 3 | Sequential runs w/o fingerprint | False FAILs on active sources; false PASS on nondeterminism |
| 4 | `EXCEPT` (distinct) comparator | False PASS on duplicate-skewed rewrites — the exact bug class this tool exists to catch |
| 5 | No prod/cost guard | Overwrites prod table or burns budget on first real run |
| 6 | 30-module scaffold pre-code | Velocity loss, over-abstracted seams before second use-case |
| 7 | `GROUP BY all columns` naive | NULL/float/struct false verdicts per warehouse |

All addressed above without expanding v1 scope.

---

## 12. Suggested immediate edits to the original doc

- Replace §17 with F1 + §7 span-surgery rule ("sqlglot never prints source").
- Replace §18 with F2 shadow-overlay spec + `cleanup` command.
- Replace §20 fallback paragraph with F3 fingerprint gate + nondeterminism denylist.
- Replace §21 class hierarchy with §5 matrix (one `compare_sql.py` + `input_guard.py`).
- Replace §22 prose with §8 SQL + float/NULL/collation rules.
- Collapse §7 layout to §6; add §9 CLI/exit-code/prod-guard contract.
- Add `reason_code` and `TOO_LARGE`/`INPUT_CHANGED`/`UNVERIFIABLE` states to §23/27
  state machine (verification can now fail-closed for operational reasons, not just data diffs).
