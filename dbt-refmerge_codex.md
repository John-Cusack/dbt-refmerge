# dbt-refmerge — Codex Architecture Review

**Reviewed:** 2026-09-15  
**Perspective:** dbt architecture, SQL semantics, developer tooling, and software safety

## Executive opinion

This is a strong product concept with the right high-level boundary and the right safety instinct. I would approve it for a prototype, but I would not call the current design “finalized” or ship automatic fixes from it yet.

The best decisions are:

- a standalone CLI rather than a dbt package;
- dbt CLI/artifacts as the compatibility boundary;
- compiled SQL for semantic inspection and source SQL for editing;
- a deliberately narrow first transformation;
- plan, verify, then apply—never edit and roll back;
- exact multiset comparison rather than row count or set equality.

The design still has four load-bearing gaps:

1. **The word “proves” is too strong.** A comparison over one data snapshot proves equality only for that snapshot and execution context. It is excellent regression evidence, but it is not proof that two programs are equivalent for every possible future input.
2. **Two sequential dbt runs do not satisfy the same-input invariant.** A row-count or metadata fingerprint cannot close this race. Both results should be evaluated in one warehouse statement, one explicit snapshot transaction, or against immutable input snapshots.
3. **Compiled-to-source position mapping is not a dependable editing strategy.** dbt does not provide a general source map from compiled SQL through arbitrary Jinja and macros. The source and compiled representations must be parsed independently and joined conservatively.
4. **The proposed rewrite depends on more lineage than v1 needs.** A scope-aware CTE reference rewrite plus a strict projection map is smaller and easier to audit than a general column-lineage subsystem.

My overall verdict is:

> **Keep the product boundary and verification-first design. Revise the guarantee, execution harness, source-editing design, and v1 scope before implementation.**

## The product promise I would use

The current promise says that the tool “proves” the output is unchanged. I would replace it with:

> **dbt-refmerge generates a conservative refactor and verifies full schema and multiset equality for the current model against the same observable input snapshot. It applies a change only when static eligibility and snapshot verification both pass.**

This distinction matters. Two implementations can happen to return identical results on today’s empty table, or on data that lacks the value exposing a bug, while differing on tomorrow’s data. The static allowlist supplies the general semantic argument; execution supplies a full-data regression check for one snapshot. The two controls complement each other, but execution is not universal program equivalence.

I would name the successful state `SNAPSHOT_EQUIVALENT`, with `VERIFIED_SAFE` retained as friendly CLI wording. The JSON result should state the exact scope of the verification.

## Recommended v1 architecture

The safest v1 does not need to materialize the original and candidate model outputs in two separate runs. It can compile both versions, expose their compiled query bodies as isolated scratch views, and compare those views in a single statement.

```text
source snapshot
    |
    +-- compile original source --------> original compiled SELECT
    |
    +-- patch source copy
            |
            +-- compile candidate ------> candidate compiled SELECT
                                              |
                         validate expected compiled-AST delta
                                              |
                                              v
                       minimal temporary dbt harness project
                         |                         |
                         v                         v
                  baseline scratch view    candidate scratch view
                         |                         |
                         +------------+------------+
                                      |
                                      v
                       one bag-difference SQL statement
                       (one observable input snapshot)
                                      |
                           +----------+----------+
                           |                     |
                           v                     v
                   SNAPSHOT_EQUIVALENT      DIFFERENT / ERROR
                           |
                           v
                         hash-bound plan
                           |
                           v
                  atomic source patch in `fix`
```

The scratch objects are views, not tables containing independently captured results. Querying both views in the same comparison statement causes both query bodies to observe the same statement-level input state on the normal warehouse execution path. It also avoids running the user model’s materialization, grants, and post-hooks merely to compare the final `SELECT` result.

This needs an explicit product boundary:

> v1 verifies the relational result of a SQL model’s compiled query. It does not verify hooks, grants, contracts as enforcement operations, physical layout, incremental mutation behavior, or other materialization side effects.

That is a clean and valuable boundary. Materialization-equivalence can be a later feature.

### Suggested execution harness

Create a minimal temporary dbt project outside the user’s repository. It should:

- use the selected profile/target credentials;
- override schema generation to an explicitly configured scratch schema;
- contain no user hooks or project-level materialization configuration;
- create uniquely named baseline and candidate views from already compiled SQL;
- run one generated comparison test/query against those views;
- use separate target and log paths;
- record every created object and drop only those exact objects during cleanup.

Before executing any DDL, inspect the harness manifest and reject the run unless every writable relation resolves inside the configured scratch database/schema. Do not infer safety from a target name such as `dev` or `prod`; names are conventions, not security boundaries.

`dbt clone` and deferral remain useful later, particularly for incremental verification, but they are not substitutes for the v1 comparison harness. dbt documents that clone behavior varies by platform and can fall back to a pointer view when zero-copy cloning is unavailable. [dbt clone documentation](https://docs.getdbt.com/reference/commands/clone)

## Critical design changes

### 1. Separate static eligibility from observed verification

Use states that distinguish semantic decisions from operational outcomes:

```text
DETECTED
  -> SOURCE_MAPPABLE
  -> MERGE_ELIGIBLE
  -> CANDIDATE_COMPILED
  -> COMPILED_DELTA_VALID
  -> COMPARISON_SUPPORTED
  -> SNAPSHOT_EQUIVALENT | DIFFERENT | UNVERIFIABLE | ERROR
  -> FIXABLE
```

`VERIFICATION_FAILED` currently conflates at least three cases:

- the outputs differ (`DIFFERENT`);
- equality cannot be evaluated for an output type (`UNVERIFIABLE`);
- compilation, permissions, timeout, or warehouse execution failed (`ERROR`).

These must have distinct reason codes and CI behavior. An operational error is not evidence of a semantic difference.

`INTENTIONAL_SLICE` also claims knowledge the tool does not have. Different predicates show different rowsets, not human intent. Prefer `DIFFERENT_PREDICATE` or `DISTINCT_ROWSET_IMPORTS`, with explanatory text saying the pattern *may* be intentional.

Likewise, `STATICALLY_EQUIVALENT` is inaccurate when two imports project different columns. `MERGE_ELIGIBLE` is the clearer state.

### 2. Make `scan` honestly warehouse-free

The current architecture says `scan` uses compiled SQL but performs no warehouse queries. Those claims conflict. dbt explicitly documents that `dbt compile` can populate the relation cache and execute introspective macros such as `run_query`; `execute` is true during compilation. `dbt parse` is warehouse-free, but its manifest does not contain compiled SQL. [dbt compile documentation](https://docs.getdbt.com/reference/commands/compile) and [dbt parse documentation](https://docs.getdbt.com/reference/commands/parse)

Choose one of these contracts:

- `scan` is source/parse-artifact only, never invokes `dbt compile`, and may conservatively report `NEEDS_COMPILE`;
- `scan --artifacts <path>` consumes caller-supplied compiled artifacts without checking their freshness;
- `scan --compile` permits warehouse metadata/introspection access but does not materialize models.

The first should be the default. The JSON output should say whether results came from source parsing, parsed artifacts, or compiled artifacts.

### 3. Do not map compiled byte spans back to Jinja source

Use a dual representation:

```text
original source
  -> Jinja-aware lexical masking
  -> source CTE/token structure with editable byte spans

compiled SQL
  -> SQLGlot with the adapter dialect
  -> semantic CTE/scope structure

manifest
  -> model identity, dependencies, ref/source metadata, relation identity

join all three on model unique_id + CTE identity + constrained ref occurrence
```

The join must be one-to-one. A missing, duplicated, reordered, macro-generated, or differently named CTE should produce `SOURCE_MAPPING_AMBIGUOUS`, never a guessed patch.

For v1, a small source lexer is reasonable if it only recognizes top-level `WITH` entries, balanced parentheses, strings, SQL comments, and Jinja blocks. SQLFluff is worth a focused spike because it supports Jinja/dbt templating and maintains source/templated slices, but adopting its internal Python APIs adds a second SQL parser and a dbt-templater compatibility surface. [SQLFluff dbt templater documentation](https://docs.sqlfluff.com/en/stable/configuration/templating/dbt.html)

Do not render a SQLGlot AST back into the user’s source. SQLGlot explicitly prioritizes semantic SQL generation over preservation of formatting, casing, and quoting. [SQLGlot project documentation](https://github.com/tobymao/sqlglot)

### 4. Replace broad lineage with a constrained projection and binding problem

For each eligible import, record an ordered map:

```text
output identifier -> upstream identifier
```

Only bare upstream columns and unambiguous aliases are allowed. The merged CTE exposes the union of these mappings. Reject collisions such as two different upstream columns producing the same output identifier.

Then rewrite only table references that are proven by SQL scope to bind to a removed CTE. Preserve the old CTE name as an alias:

```sql
-- before
from order_financials

-- after
from orders as order_financials
```

If an alias already exists, retain it and change only the bound relation token:

```sql
-- before
from order_financials as f

-- after
from orders as f
```

This avoids globally replacing `order_financials.amount` with `orders.amount`, preserves downstream qualifiers, and substantially reduces the lineage burden.

Reject `*` or `cte.*` against **any** affected CTE, including the canonical CTE. Adding columns to the canonical CTE can otherwise change a downstream star expansion even if the removed CTE is never referenced with a star.

SQLGlot’s scope and lineage utilities are useful building blocks, but its own documentation notes that qualification can require a database schema to disambiguate columns. Wrap the subset you use behind project-owned code and test it as a safety boundary. [SQLGlot AST and scope primer](https://github.com/tobymao/sqlglot/blob/main/posts/ast_primer.md)

### 5. Verify the generated source, not only the conceptual plan

After patching the temporary source:

1. compile the candidate under the same dbt executable, profile, target, vars, environment, and package state as the baseline;
2. parse both compiled queries with the same SQLGlot version and dialect;
3. confirm that their AST delta is exactly the planned transformation;
4. reject any additional compiled change;
5. run the warehouse comparison using the compiled candidate that came from the patched source.

This gate catches a source edit that accidentally crosses a Jinja boundary or changes how a macro renders.

Compile context should be captured in the result: dbt executable/version, manifest schema version, adapter/type, target name, non-secret vars digest, package-lock digest, relevant environment-variable digest, SQLGlot version, and source hashes. Never persist secret values.

## Answers to the 20 review questions

### 1. Is a standalone Python CLI the correct boundary?

**Yes.** This is a developer tool and orchestrator, not reusable dbt SQL. Independent installation avoids changing `packages.yml`, works before a project chooses to adopt a package, and gives the tool control over temporary files, reporting, and source edits.

One packaging detail is important: `uv tool install` isolates the Python package, while the selected `dbt` executable normally comes from the user’s PATH or project environment. Make that explicit and support `--dbt-command`, then record the resolved executable and `dbt --version` output.

### 2. Is dbt CLI plus artifacts preferable to dbt-core internals?

**Yes, especially now that more than one dbt engine/version line exists.** Subprocess isolation is a feature, not merely a compromise. dbt’s own programmatic-invocation documentation warns that concurrent invocations in one process are unsafe in dbt Core and recommends separate processes for safe parallel execution. [dbt programmatic invocation documentation](https://docs.getdbt.com/reference/programmatic-invocations)

Treat artifacts as versioned external schemas. Validate their `metadata.dbt_schema_version`, maintain a compatibility matrix, and fail with a useful unsupported-version message. Do not silently read fields by assumption.

### 3. Is the compiled/source distinction correct?

**Yes.** It is fundamental. Source is the editable representation; compiled SQL is an observation of one dbt compilation context. The manifest supplies identity and graph information, but it is not a lossless source map.

Also preserve the exact baseline source bytes. `fix` should use a compare-and-swap write: apply only if the current file hash still equals the analyzed file hash.

### 4. Safest compiled-AST to source/Jinja mapping?

**Do not perform direct positional mapping.** Build source spans independently using Jinja-aware lexical masking, build semantic nodes independently from compiled SQL, and accept only a unique structural match. Any target CTE containing Jinja control flow, macro-generated projections, or macro-generated CTE structure should be report-only in v1.

### 5. Is SQLGlot sufficient for v1?

**Yes, for the constrained compiled-SQL subset.** It is a good parser and AST toolkit, not a warehouse validator or source-preserving concrete syntax tree. Pin and report its version, select the explicit input dialect, treat warnings/fallback command nodes as unsupported, and run real-warehouse parser fixtures. SQLGlot describes its parser as intentionally lenient, which is another reason successful parsing cannot itself be a safety signal. [SQLGlot project documentation](https://github.com/tobymao/sqlglot)

### 6. Internal lineage or library?

**Build only the minimal resolver internally on top of SQLGlot scopes.** Do not build general project-wide lineage in v1 and do not add another lineage library yet. The necessary problem is CTE binding plus direct projection mapping. Extract an interface so a more capable resolver can replace it later.

### 7. Is exact AST predicate equivalence appropriate?

**Yes.** Normalize only syntax that cannot change meaning: whitespace, comments, redundant parentheses where the parser already removes them, and dialect-correct unquoted identifier casing. Do not reorder `AND`/`OR`, commute comparisons, fold constants, or apply symbolic algebra in v1.

Compare predicates after resolving their single input scope. A syntactically identical bare name is not enough if it binds differently.

### 8. Is the import CTE definition narrow enough?

**Almost; express it as an allowlist, not a denylist.** The v1 grammar should require:

- one non-recursive query block;
- one literal `ref()` or `source()` relation in `FROM`;
- no other table-producing construct;
- projections consisting only of a bare input column or bare input column with an explicit alias;
- unique, statically known output names;
- no wildcard anywhere that observes an affected CTE;
- zero or one `WHERE` using an allowlisted deterministic expression subset;
- no other relational clause, CTE materialization hint, Jinja control flow, or execute-time macro inside the target CTE.

This excludes scalar functions in projections, even deterministic ones, for v1. That restraint is appropriate.

### 9. Best temporary candidate project architecture?

**Use an OS temporary workspace containing a materialized copy/snapshot, not `.dbt-refmerge/` under a dbt model path.** The copy should exclude `.git`, logs, `target`, virtual environments, and caches while preserving project files, package state, newline modes, file modes, and untracked models. Editable files must never be hard-linked or symlinked back to the working tree; reflink copies are acceptable when their copy-on-write behavior is guaranteed.

Local package dependencies outside the project directory are an edge case that must be detected and mirrored with their relative layout. Do not automatically run `dbt deps` and change package resolution during a verification; use the captured lock/package state or fail with instructions.

A Git worktree can be an optimization for a clean, fully tracked repository, but it cannot be the correctness mechanism because dbt projects can contain uncommitted and untracked work or no Git repository at all.

### 10. How should baseline execute without overwriting the real relation?

**Do not run the real model materialization in v1.** Compile the original query and install it as a uniquely named view in the explicit scratch schema through the minimal harness. Preflight the resolved relation name before creation.

If the product later needs to test materialization semantics, that should be a separate mode with a stronger adapter contract. A forced `materialized='table'` run is not equivalent to the original incremental/view/table materialization and should not be described as such.

### 11. How should the proposed model run under a different name?

Compile the patched source under the same original model path and identity in the candidate copy, then install its compiled query as the second scratch view in the harness. This keeps `model`, `this`, target, vars, and package behavior as aligned as possible during compilation.

Reject models whose query semantics depend on the destination relation (`this`), `is_incremental()`, invocation-specific values, or other context that the harness cannot reproduce.

### 12. Best same-input guarantee?

**Run the full comparison as one SQL statement over the two views.** This is simpler and stronger than sequential materializations. Where one-statement comparison is impossible, use a single explicit snapshot/repeatable-read transaction on one connection or immutable snapshots/clones of every leaf input.

Do not call `count(*)`, `max(updated_at)`, checksums, or `last_altered` timestamps a guarantee. Data can change while retaining all of those values, and fingerprinting itself has a race unless tied to a snapshot.

External tables, streaming buffers, remote functions, APIs, and other non-transactional inputs need explicit unsupported or weaker-guarantee statuses.

### 13. How should isolation differ by warehouse?

| Warehouse | Preferred v1 path | Stronger/later path | Important limitation |
|---|---|---|---|
| Snowflake | Two scratch views, one comparison statement | Time Travel or database/schema/table clone at a recorded point | External tables are not cloned; fully qualified references in views can still point outside a cloned namespace. [Snowflake clone documentation](https://docs.snowflake.com/en/sql-reference/sql/create-clone) |
| BigQuery | Two scratch views, one comparison query | Multi-statement snapshot transaction or table snapshots/clones at one time | External data is not guaranteed consistent in a transaction; snapshots have source/type/location limits. [BigQuery transaction documentation](https://cloud.google.com/bigquery/docs/transactions) |
| Databricks | Two scratch views, one SQL statement | Pin every Delta input to a version/timestamp or use shallow/deep clones | Guarantees depend on Delta/Iceberg/Parquet and Unity Catalog capabilities; external/non-versioned inputs differ. [Databricks table history](https://docs.databricks.com/aws/en/tables/history) |
| Redshift | Two scratch views, one statement | One connection and one SNAPSHOT/SERIALIZABLE transaction | Separate dbt subprocesses cannot share the same transaction. [Redshift isolation documentation](https://docs.aws.amazon.com/redshift/latest/dg/c_serial_isolation.html) |
| Postgres | Two scratch views, one statement | One connection with `REPEATABLE READ`, optionally temporary relations | The stable snapshot belongs to one transaction/connection. [PostgreSQL isolation documentation](https://www.postgresql.org/docs/current/transaction-iso.html) |

Support one warehouse first. Snowflake is a pragmatic first choice if there is no customer signal because its cloning and scratch-object capabilities leave room for incremental verification, but the right commercial choice should follow the first design partners.

### 14. Best warehouse-independent multiset equality?

Define equality as:

```text
same ordered output schema
AND baseline_only_occurrences = 0
AND candidate_only_occurrences = 0
```

Use `EXCEPT ALL` in both directions only on adapters and output types that support it correctly. BigQuery supports `EXCEPT DISTINCT`, not `EXCEPT ALL`; Redshift also does not support `EXCEPT ALL`. [BigQuery set-operator syntax](https://cloud.google.com/bigquery/docs/reference/standard-sql/query-syntax#set_operators) and [Redshift set-operator syntax](https://docs.aws.amazon.com/redshift/latest/dg/r_UNION.html)

The portable fallback is a signed bag aggregate:

```sql
with tagged as (
    select <all columns>,  1 as __delta from baseline
    union all
    select <all columns>, -1 as __delta from candidate
),
differences as (
    select <all columns>, sum(__delta) as __delta
    from tagged
    group by <all columns>
    having sum(__delta) <> 0
)
select
    coalesce(sum(case when __delta > 0 then __delta else 0 end), 0)
        as baseline_only_occurrences,
    coalesce(sum(case when __delta < 0 then -__delta else 0 end), 0)
        as candidate_only_occurrences
from differences
```

`GROUP BY` groups null values appropriately for this use; the harder issues are non-groupable complex types, floating-point `NaN`/signed-zero behavior, collations, semi-structured canonicalization, geography, and adapter coercion. Maintain a per-adapter comparable-type capability table. If a type lacks exact semantics, return `UNSUPPORTED_COMPARISON_TYPE`; do not hash it and call the result deterministic. Hash comparison can be an explicitly probabilistic fast mode, never the basis for automatic `fix`.

Schema equality should compare ordered column names and adapter-normalized exact types. “Compatible” types are not enough for a strict refactor. Define separately whether nullability, collation, policy tags, comments, and other relation metadata are in scope; v1 can reasonably limit the claim to query output names, order, and types.

### 15. Should an existing equality package be used?

**Not as the safety kernel.** Keep a small project-owned comparator interface and use existing packages as references and cross-tests.

The current `dbt_utils.equality` implementation uses `EXCEPT` in both directions, which is distinct-set comparison and therefore does not establish bag equality when duplicate multiplicities differ. [dbt-utils equality source](https://github.com/dbt-labs/dbt-utils/blob/main/macros/generic_tests/equality.sql)

`dbt-audit-helper` is valuable for diagnostics and primary-key-oriented investigation, but its comparison interfaces and output contract are not the exact, dependency-free safety primitive this CLI needs. [dbt-audit-helper documentation](https://github.com/dbt-labs/dbt-audit-helper)

Use differential tests that run the internal comparator against adversarial datasets: duplicate-count swaps, all-null rows, NaN/infinity, signed zero, timestamps, collations, binary, arrays/structs/variant, and very wide rows.

### 16. Incremental and ephemeral models in v1?

**Exclude both as direct rewrite targets.** A full-refresh build of an incremental model verifies the full-refresh branch, not the incremental transition, unique-key behavior, merge predicates, schema-change policy, or current destination state. Calling that model “verified” is misleading.

A later incremental verifier should:

1. capture or clone the current destination relation into two isolated writable relations;
2. pin all upstream inputs to one snapshot;
3. execute the original and candidate incremental transitions against the two clones;
4. compare the final relations and relevant schema/metadata.

Ephemeral models have no standalone relation. Later, verify their effect through selected materialized children. An ordinary materialized model that merely depends on an unchanged ephemeral parent can still be eligible if its final compiled query meets all other rules.

### 17. Is source-preserving rewriting practical?

**Yes for the narrow v1 subset.** It is practical only if the source parser owns byte spans and edits are token-aware splices. A templating-aware concrete syntax tree may become worthwhile as supported syntax expands, but it is not mandatory for a strict first version.

Preserve encoding, BOM, newline style, final newline, permissions, and untouched bytes exactly. Apply edits from the end of the file toward the beginning. Re-lex and recompile the result before considering the plan valid.

Never use regular-expression global replacement for identifiers. SQL scope, nested CTE shadowing, comments, string literals, and quoted identifiers make it unsafe.

### 18. Can verification incorrectly claim equality?

**Yes.** Important false-confidence cases include:

- the current snapshot lacks values that expose a semantic defect;
- both outputs are empty;
- baseline and candidate compile under different invocation context;
- volatile functions, sequences, remote functions, session variables, or environment-dependent macros are present;
- an approximate/hash/sample comparison is labeled exact;
- duplicate multiplicity is lost through distinct set operations;
- implicit coercion in the comparison query hides a schema/type difference;
- unsupported values have non-total equality semantics (`NaN`, variants, collations, geography);
- row-level policies or masking differ for scratch objects;
- external inputs change independently of the warehouse snapshot;
- only a limited number of differences or rows is evaluated;
- individual rewrites pass separately, but the combined source written by `fix` was never verified.

Verification must run against the final **combined candidate per model**, not only each duplicate group in isolation.

### 19. Security and warehouse-safety issues?

Treat the dbt project as trusted executable code. Compilation itself can execute introspective SQL and macros. The tool cannot sandbox arbitrary dbt/Jinja behavior while also promising to reproduce the user’s environment.

Required controls include:

- explicit scratch database/schema configuration;
- a manifest preflight proving every write target is inside that boundary;
- least-privilege credentials where possible;
- unique, high-entropy object names plus ownership/query tags or labels;
- statement timeouts, cancellation handling, concurrency limits, and warehouse-specific cost controls such as BigQuery maximum bytes billed;
- exact-object cleanup in `finally`, plus TTL/lifecycle cleanup for crashes;
- no `DROP SCHEMA ... CASCADE` unless the tool created that exact empty schema in the current run and has revalidated its identity;
- identifier construction through dbt relations/quoting, never raw unvalidated interpolation;
- secret redaction from commands, logs, JSON, exception messages, and saved artifacts;
- symlink/path traversal checks before reading or patching files;
- an advisory filesystem lock and source-hash compare-and-swap before `fix`;
- no assumption that a Git repository exists or is clean.

A target-name denylist such as “refuse target `prod`” is not sufficient. A target named `dev` can still point at production, while production reads with isolated scratch writes may be a legitimate verification configuration.

### 20. Does the design generalize without overengineering v1?

**Yes, if it generalizes around a transformation protocol rather than around dozens of modules.** The reusable interface is approximately:

```python
class Transformation(Protocol):
    def detect(self, context) -> list[Finding]: ...
    def qualify(self, finding, context) -> Eligibility: ...
    def plan(self, finding, context) -> RewritePlan: ...
    def patch(self, plan, source: bytes) -> bytes: ...
```

Compilation, compiled-delta validation, snapshot comparison, receipts, reporting, and atomic application are shared services. Do not build a plugin registry or generic rewrite DSL until a second transformation demonstrates the needed abstraction.

## Recommended v1 rewrite contract

The transformation itself should have these additional rules:

- Choose the canonical CTE deterministically: first eligible source occurrence is simplest and minimizes CTE ordering surprises.
- Union the **declared direct projection mappings**, not inferred downstream usage. The latter requires broader lineage and saves little in v1.
- Preserve old downstream qualifier names as aliases when redirecting table references.
- Reject output-name collisions, quoted/unquoted ambiguity, nested shadowing that cannot be proven, recursive CTEs, and affected star expansion.
- Compare normalized identifiers using adapter rules; do not blindly lowercase everything.
- Verify all planned merges in a model as one final candidate.

An even safer optional rewrite form is to introduce one internal import CTE and retain the original CTEs as thin projections from it. That preserves downstream CTE interfaces and avoids reference rewriting, at the cost of an extra CTE and a less dramatic diff. It may be a useful `--preserve-cte-interfaces` mode, but I would first validate which output style users actually want.

## Recommended domain model changes

Use immutable dataclasses for internal domain objects and reserve Pydantic for configuration/JSON boundaries if needed.

```python
@dataclass(frozen=True)
class SourceSpan:
    start_byte: int
    end_byte: int


@dataclass(frozen=True)
class ImportProjection:
    upstream_identifier: str
    output_identifier: str
    source_span: SourceSpan


@dataclass(frozen=True)
class RewritePlan:
    model_unique_id: str
    source_path: str
    source_sha256: str
    canonical_cte: str
    removed_ctes: tuple[str, ...]
    projections: tuple[ImportProjection, ...]
    edits: tuple[TextEdit, ...]
    reason_codes: tuple[str, ...]


class VerificationStatus(str, Enum):
    NOT_RUN = "not_run"
    SNAPSHOT_EQUIVALENT = "snapshot_equivalent"
    DIFFERENT = "different"
    UNVERIFIABLE = "unverifiable"
    ERROR = "error"


@dataclass(frozen=True)
class VerificationReceipt:
    plan_sha256: str
    original_source_sha256: str
    candidate_source_sha256: str
    original_compiled_sha256: str
    candidate_compiled_sha256: str
    dbt_version: str
    adapter_type: str
    comparator_version: str
    schema_equal: bool
    baseline_rows: int
    candidate_rows: int
    baseline_only_occurrences: int
    candidate_only_occurrences: int
    status: VerificationStatus
```

Do not store `verified` as an independently settable Boolean; derive it from the status and exact zero-difference invariants.

`fix` should rerun the pipeline by default and write only if the verification receipt matches the exact candidate bytes. If cached receipts are ever supported, accept them only when bound to immutable input snapshot identities and every source/config/tool digest.

## Package structure recommendation

The proposed package tree is clean conceptually but premature for v1. About 30 implementation modules and five adapter classes will slow navigation before the boundaries are proven. Start closer to:

```text
src/dbt_refmerge/
├── cli.py
├── config.py
├── domain.py
├── dbt_cli.py
├── artifacts.py
├── source.py
├── semantics.py
├── analyze.py
├── rewrite.py
├── verification/
│   ├── harness.py
│   ├── comparator.py
│   └── capabilities.py
└── reporting.py
```

Split modules when independent implementations or size make the separation real. In particular, start with one supported adapter and a capability record rather than five empty subclasses.

The proposed test layout is good. Add these categories:

- artifact compatibility fixtures for each supported dbt manifest schema;
- adversarial source-mapping tests with Jinja whitespace control, comments, nested CTEs, quoted names, and shadowing;
- metamorphic tests that randomly vary formatting without changing the plan;
- comparator conformance tests against every supported warehouse/type;
- crash/timeout/cleanup tests;
- source race tests proving `fix` refuses changed bytes;
- combined-plan tests with multiple duplicate groups in one model;
- property tests proving edits never touch bytes outside declared spans.

DuckDB is useful for fast pipeline tests, but it cannot certify Snowflake, BigQuery, Databricks, Redshift, or Postgres comparison semantics. At least one real integration lane is required for each advertised adapter.

## CLI and UX recommendations

Keep `scan`, `check`, and `fix`, with these semantics:

```text
scan   source/parse analysis; warehouse-free by default
check  compile, build scratch views, exact compare, report; no source writes
fix    rerun check, atomically apply the exact verified candidate
```

`fix --dry-run` should still say whether the displayed diff was statically generated only or snapshot-verified. Avoid showing an unverified diff in a way that resembles an approved fix.

For machine use:

- reserve stdout for JSON when `--json` is set and send logs to stderr;
- version the JSON schema;
- define stable reason codes independently of prose;
- distinguish “findings exist,” “verified fixes exist,” “outputs differ,” “unsupported,” and “tool error” in exit-code policy;
- print estimated/actual warehouse cost metadata where an adapter exposes it;
- show scratch objects and cleanup status;
- state the exact guarantee: model query result, snapshot/context, schema scope, and comparator strategy.

The tool should not claim a performance improvement merely because imports were consolidated. Warehouse optimizers may inline, reuse, or materialize CTEs differently, and a wider multiply referenced CTE can be slower. Treat performance as unmeasured unless a separate benchmark/profile mode demonstrates otherwise.

## Suggested implementation sequence

1. **Source-only scanner:** top-level CTE spans, literal `ref`/`source` recognition, manifest identity, reason-coded findings.
2. **Compiled semantic gate:** SQLGlot dialect parsing, import allowlist, exact predicate comparison, scope-aware reference binding.
3. **Source patcher:** token-span edits, alias-preserving reference changes, golden/property tests, candidate recompilation, compiled-AST delta validation.
4. **One-warehouse harness:** scratch view preflight, exact schema inspection, one-statement bag comparison, cost/timeout/cleanup controls.
5. **Atomic `fix`:** full combined candidate verification, receipt binding, source hash check, atomic replace.
6. **CI/JSON hardening:** schema-versioned output, exit codes, artifact/version compatibility.
7. **Only then:** second warehouse, then incremental-transition verification, then a second refactoring rule.

I would not implement full project-wide lineage, five warehouse adapter classes, or incremental models before the first end-to-end exact path works on one warehouse.

## Final assessment

The architecture has a credible differentiator and a disciplined safety posture. The standalone boundary, dbt delegation, dual source/compiled representation, and fail-closed rewrite rules are all correct.

The central refinement is conceptual as much as technical:

> Static rules establish that a rewrite belongs to a tiny class the tool knows how to transform. A one-snapshot, full multiset comparison supplies strong empirical verification. Neither should be overstated, and both must be bound to the exact source bytes that `fix` writes.

With the one-statement comparison harness, conservative independent source mapping, an allowlisted import grammar, and v1 exclusion of incremental/ephemeral targets, this is implementable without recreating dbt or overengineering a general SQL refactoring engine.

## Primary references consulted

- [dbt compile](https://docs.getdbt.com/reference/commands/compile)
- [dbt parse](https://docs.getdbt.com/reference/commands/parse)
- [dbt programmatic invocations](https://docs.getdbt.com/reference/programmatic-invocations)
- [dbt deferral](https://docs.getdbt.com/reference/node-selection/defer)
- [dbt clone](https://docs.getdbt.com/reference/commands/clone)
- [SQLGlot](https://github.com/tobymao/sqlglot)
- [SQLFluff dbt templater](https://docs.sqlfluff.com/en/stable/configuration/templating/dbt.html)
- [dbt-utils equality implementation](https://github.com/dbt-labs/dbt-utils/blob/main/macros/generic_tests/equality.sql)
- [dbt-audit-helper](https://github.com/dbt-labs/dbt-audit-helper)
- [Snowflake cloning](https://docs.snowflake.com/en/sql-reference/sql/create-clone)
- [BigQuery transactions](https://cloud.google.com/bigquery/docs/transactions)
- [Databricks table history/time travel](https://docs.databricks.com/aws/en/tables/history)
- [Redshift transaction isolation](https://docs.aws.amazon.com/redshift/latest/dg/c_serial_isolation.html)
- [PostgreSQL transaction isolation](https://www.postgresql.org/docs/current/transaction-iso.html)
