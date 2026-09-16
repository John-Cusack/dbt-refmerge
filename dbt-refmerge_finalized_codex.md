# dbt-refmerge — Final Architecture

**Status:** implementation baseline  
**Finalized:** 2026-09-15  
**Scope:** conservative v1 for verified consolidation of duplicate dbt import CTEs

## 1. Executive decision

`dbt-refmerge` will be a standalone Python CLI that analyzes an existing dbt project, identifies a narrowly defined class of duplicate import CTEs, constructs a source-preserving candidate, compiles both versions with the user’s dbt installation, and applies a change only after exact schema and multiset equality are established for one shared observable input snapshot.

The architecture is approved with these non-negotiable decisions:

- The product remains outside dbt and invokes the user-selected `dbt` executable in subprocesses.
- `scan` is warehouse-free by default and does not depend on compiled SQL.
- Source SQL owns editable byte spans; compiled SQL owns semantic analysis. Compiled offsets are never treated as source offsets.
- SQLGlot is the v1 compiled-SQL parser, under a strict allowlist and explicit dialect.
- v1 implements CTE binding and direct projection mapping, not general column lineage.
- Baseline and candidate query bodies are exposed as scratch views and compared in one warehouse statement.
- Sequential runs plus input fingerprints are not accepted as a same-input guarantee.
- Equality is exact ordered-schema equality plus bidirectional bag equality. Hashes, sampling, tolerances, and `EXCEPT DISTINCT` cannot authorize `fix`.
- Incremental, ephemeral, and Python models are not automatic rewrite targets in v1.
- Warehouse writes are permitted only inside an explicit, preflighted scratch boundary.
- `fix` verifies the final combined candidate and atomically writes only the exact bytes bound to its verification receipt.

## 2. Reconciliation of the two architecture reviews

The two reviews agree on the product boundary, source/compiled split, conservative grammar, SQLGlot, minimal lineage, smaller package structure, compiled-delta gate, exact bag comparison, and stronger warehouse safety.

Where they disagree, this architecture resolves the decisions as follows:

| Topic | Final decision | Reason |
|---|---|---|
| Same input | One comparison statement over two scratch views | A count or metadata fingerprint can miss updates and still has a race. It is a detector, not isolation. |
| Candidate workspace | External source snapshot/candidate copy plus a separate minimal dbt harness | In-project shadow files mutate the project’s discovered node set and can inherit project hooks/config. A correct copy can preserve packages and local dependencies. |
| Verification objects | Scratch views, not two sequentially populated tables | Views let the final comparison evaluate both query bodies under one statement snapshot. |
| `EXCEPT ALL` | Capability-tested per adapter; signed-delta grouped counts are the portable fallback | BigQuery and Redshift do not support `EXCEPT ALL`; current Snowflake syntax documents `MINUS`/`EXCEPT` as distinct set difference. |
| `GROUP BY` and nulls | Nulls are valid for grouped bag counting; complex types and floating-point behavior require capability rules | SQL grouping puts nulls into one group. The real portability problems are groupability and type-specific equality. |
| Incremental v1 | Report but do not auto-rewrite | An isolated full refresh does not verify incremental transition semantics. |
| Production safety | Explicit scratch boundary and resolved-relation preflight | Target names such as `prod` and `dev` are conventions, not security boundaries. |
| Dirty Git tree | Allowed if the exact target bytes have not changed | The tool must support uncommitted work and non-Git projects. Source hash compare-and-swap is the real safety control. |
| Reference rewriting | Scope-bound token edits that preserve the removed CTE name as an alias | Regex/global qualifier replacement is unsafe and unnecessary. |
| Float tolerance | Diagnostic-only future option, never an automatic-fix gate | Tolerance contradicts the promise that output is unchanged. |
| Cleanup | Exact recorded objects, with ownership metadata and TTL | Prefix matching or broad `DROP ... CASCADE` is too dangerous. |

## 3. Product definition and guarantee

### 3.1 Product boundary

Installation remains independent of the dbt project:

```bash
uv tool install dbt-refmerge
```

Usage:

```bash
cd company_dbt_project
dbt-refmerge scan
dbt-refmerge check --scratch-schema dbt_refmerge_alice
dbt-refmerge fix models/marts/fct_orders.sql --scratch-schema dbt_refmerge_alice
```

The tool delegates to dbt for:

- project parsing and manifest production;
- Jinja and macro rendering;
- `ref()` and `source()` resolution;
- adapter selection and authentication;
- package behavior;
- dialect-specific compilation.

It does not recreate dbt’s compiler or adapter runtime.

### 3.2 Exact guarantee

The public promise is:

> **dbt-refmerge generates a conservative refactor and verifies exact output-schema and multiset equality for the model query against one shared observable input snapshot and compilation context. It applies only the exact candidate that passed both static qualification and this verification.**

The successful machine status is `SNAPSHOT_EQUIVALENT`. The CLI may render this as `VERIFIED SAFE`, but it must also state the verification scope.

The tool does not claim universal program equivalence. Equality on today’s data cannot prove equality for every future dataset. The safety case is the combination of:

1. a small transformation with a reviewable static semantic argument; and
2. a full-data regression comparison on one controlled snapshot.

### 3.3 Explicit non-guarantees in v1

v1 verifies the relational result of the compiled SQL query. It does not verify equivalence of:

- pre-hooks, post-hooks, or `on-run-*` hooks;
- grants, comments, tags, policy attachments, or physical layout;
- clustering, partitioning, indexes, sort/distribution keys, or performance;
- incremental mutations or schema-change policy;
- external API/remote-function behavior;
- relation row order, because a table is a multiset unless order is part of an outer query result.

Duplicate CTE consolidation is not automatically a performance optimization. Warehouses can inline, reuse, or materialize CTEs differently, and a wider multiply referenced CTE can be slower. Performance requires a separate measurement mode and is never implied by `VERIFIED SAFE`.

## 4. Commands and behavioral contract

### 4.1 `scan`

```bash
dbt-refmerge scan
```

Default behavior:

- reads source files and may invoke warehouse-free `dbt parse`;
- detects source-level duplicate imports and source-mapping eligibility;
- performs no warehouse connection, metadata query, model materialization, or source edit;
- reports when compiled semantic qualification is still required.

dbt documents that `dbt parse` does not connect to the warehouse and that its manifest does not contain compiled SQL. By contrast, `dbt compile` can populate relation caches and execute introspective queries. [dbt parse](https://docs.getdbt.com/reference/commands/parse) and [dbt compile](https://docs.getdbt.com/reference/commands/compile)

Optional modes:

```bash
dbt-refmerge scan --artifacts path/to/artifacts
dbt-refmerge scan --compile
```

`--artifacts` consumes supplied artifacts and reports their freshness as unknown unless a source/config digest matches. `--compile` permits dbt compilation and possible warehouse introspection, but still does not materialize models.

### 4.2 `check`

```bash
dbt-refmerge check --scratch-schema dbt_refmerge_alice
```

`check` performs the full pipeline:

```text
source snapshot
  -> source detection
  -> baseline compilation
  -> semantic qualification
  -> candidate source patch
  -> candidate compilation
  -> compiled-AST delta validation
  -> scratch-boundary preflight
  -> scratch view creation
  -> exact schema comparison
  -> one-statement multiset comparison
  -> report and verification receipt
```

It never changes the user’s source files. It may create explicitly reported scratch warehouse objects.

### 4.3 `fix`

```bash
dbt-refmerge fix models/marts/fct_orders.sql \
  --scratch-schema dbt_refmerge_alice
```

`fix` reruns `check` by default, verifies the final combined candidate for each model, and applies a patch only when:

- status is `SNAPSHOT_EQUIVALENT`;
- the receipt is bound to the exact candidate bytes;
- the current source hash still equals the analyzed source hash;
- every source edit remains inside its declared byte span.

The write is an atomic replace in the same filesystem, preserving permissions, encoding, BOM, newline style, and final-newline state.

```bash
dbt-refmerge fix --dry-run
```

`--dry-run` writes nothing and clearly labels whether the shown diff is only statically eligible or also snapshot-verified.

### 4.4 `cleanup`

```bash
dbt-refmerge cleanup --run-id <run_id>
```

Cleanup uses the run ledger and warehouse ownership metadata to delete exact recorded objects. An age-based garbage collector may operate only on objects carrying verifiable dbt-refmerge ownership metadata. It must not discover targets by name prefix alone or issue an unvalidated broad cascade.

### 4.5 JSON and exit behavior

With `--json`, stdout contains only versioned JSON; diagnostics go to stderr. Every non-successful candidate includes a stable reason code independent of human prose.

Use a small exit contract:

- `0`: command completed and the selected `--fail-on` policy was not triggered;
- `1`: tool, configuration, compilation, permission, or warehouse operational error;
- `2`: expected analysis/verification outcome triggered the selected policy.

Support `--fail-on never|finding|fixable|different|unverifiable` rather than assigning a growing list of bespoke exit codes.

## 5. dbt integration boundary

The default boundary is:

```text
dbt-refmerge
    |
    | subprocess with argv list
    v
user-selected dbt executable
    |
    v
user's engine, adapter, profile, packages, and warehouse
```

Subprocess isolation supports independently installed dbt environments and avoids coupling the package to undocumented internals. dbt’s programmatic-invocation documentation also notes that concurrent dbt Core invocations in one process are unsafe and recommends separate processes. [dbt programmatic invocations](https://docs.getdbt.com/reference/programmatic-invocations)

Required CLI controls:

- `--dbt-command` for the executable or wrapper;
- `--project-dir`, `--profiles-dir`, and `--target`;
- repeatable pass-through vars and supported global flags;
- unique target/log paths for every invocation;
- captured stdout/stderr with secret redaction;
- cancellation and timeout propagation;
- no shell invocation or command-string interpolation.

At startup, record:

- resolved dbt executable;
- `dbt --version` output;
- adapter type;
- manifest schema URI/version;
- SQLGlot version and selected dialect.

Artifacts are versioned external contracts. The loader validates their schema version and uses an explicit tested compatibility matrix. No hard-coded dbt version range is claimed until CI proves it.

## 6. Representations and source mapping

### 6.1 Source representation

Original `.sql` bytes are authoritative for:

- editable locations;
- CTE and identifier spelling;
- Jinja text;
- comments and formatting;
- the final patch.

The source layer performs Jinja-aware lexical masking without rendering. It recognizes strings, quoted identifiers, SQL comments, Jinja expressions/comments/control blocks, balanced parentheses, and top-level `WITH` entries. It records byte offsets, not character offsets.

### 6.2 Compiled representation

Compiled SQL is authoritative for:

- the executed relational structure;
- dialect-specific AST nodes;
- CTE scopes and table binding;
- resolved predicates and projections;
- the query bodies installed in the verification harness.

SQLGlot is appropriate for this constrained role. It is not a source-preserving concrete syntax tree or warehouse validator; its parser is intentionally lenient and its generator does not promise original formatting. [SQLGlot](https://github.com/tobymao/sqlglot)

Requirements:

- always pass the explicit adapter dialect;
- fail on parse errors, warning/fallback nodes, or unsupported syntax;
- pin and report the SQLGlot version;
- verify behavior with real dialect fixtures;
- never serialize SQLGlot ASTs into user source.

### 6.3 Manifest representation

The manifest supplies:

- model `unique_id`;
- resource type and materialization config;
- dependency/ref/source metadata;
- compiled code where produced;
- relation identities and target context;
- dbt and artifact schema versions.

Grouping uses dbt resource `unique_id`, not a raw relation string, whenever resolution is unambiguous.

### 6.4 Mapping rule

There is no general direct mapping from compiled AST offsets to source/Jinja offsets. The representations are joined independently:

```text
source.sql
  -> Jinja-aware lexer
  -> [(source CTE identity, ref occurrence, byte spans)]

compiled SQL
  -> SQLGlot
  -> [(semantic CTE identity, relation binding, projection, predicate)]

manifest
  -> unique ids and dependency identities

join key
  -> model unique_id
   + dialect-aware CTE identifier
   + constrained ref/source occurrence
   + source/compiled ordinal as a consistency check
```

The match must be one-to-one. Identifier comparison follows adapter quoting and case-folding rules; it does not blindly lowercase names. Any macro-generated, reordered, duplicated, missing, or ambiguous structure produces `SOURCE_MAPPING_AMBIGUOUS`.

SQLFluff is a possible future source-mapping dependency because its dbt templater tracks source and rendered slices, but its internal API and dbt compatibility surface should be evaluated in a spike before adoption. [SQLFluff dbt templater](https://docs.sqlfluff.com/en/stable/configuration/templating/dbt.html)

## 7. Detection and static qualification

### 7.1 Source detection

For every selected SQL model:

```text
enumerate top-level CTEs
  -> locate literal ref()/source() occurrences
  -> identify source-level import shapes
  -> resolve each occurrence through manifest dependencies
  -> group by upstream unique_id
```

A group with two or more imports is a finding, not necessarily a defect.

### 7.2 v1 import CTE grammar

An import CTE is merge-eligible only when every rule holds:

- the target is a non-incremental SQL table or view model;
- the CTE is a non-recursive single `SELECT` query block;
- `FROM` contains exactly one literal `ref()` or `source()` and no other table-producing construct;
- each select item is an unqualified bare upstream column, optionally with one explicit output alias;
- output identifiers are statically known and unique;
- there is zero or one `WHERE` clause;
- the predicate contains only allowlisted deterministic Boolean/comparison operations, bare columns, and literals;
- no Jinja control flow or macro-generated SQL exists inside the CTE body other than the literal `ref()`/`source()` call;
- there is no `JOIN`, aggregation, `GROUP BY`, `HAVING`, `DISTINCT`, window, `QUALIFY`, `ORDER BY`, `LIMIT`, `OFFSET`, `FETCH`, set operation, subquery, lateral construct, pivot/unpivot, table sample, materialization hint, or transformation expression;
- no downstream `*` or `affected_cte.*` can observe an affected CTE after columns are added;
- every reference to a removed CTE can be uniquely bound and source-mapped.

This is an allowlist. Unknown AST nodes do not fall through as safe.

### 7.3 Projection map

Each eligible CTE produces an ordered map:

```text
output identifier -> upstream identifier
```

The merged CTE uses the stable union of all declared projection maps, preserving the canonical CTE’s original order and appending first-seen missing outputs in source order.

If two projections map the same output identifier to different upstream identifiers, classification is `PROJECTION_COLLISION`. If mappings are identical, they are deduplicated.

The tool intentionally unions declared projections rather than computing only columns proven to be used downstream. This removes the need for general column lineage and better preserves each original CTE interface.

### 7.4 Predicate comparison

Predicates are compared as dialect ASTs after resolving their single input scope. Normalization may remove only semantically irrelevant representation differences already established by parsing:

- whitespace and comments;
- keyword presentation;
- redundant parentheses handled by the parser;
- dialect-correct normalization of unquoted identifiers.

v1 does not reorder Boolean operands, commute comparisons, fold constants, or apply symbolic equivalence. These may be rejected as different:

```sql
a = 1 and b = 2
```

```sql
b = 2 and a = 1
```

False negatives are acceptable. False positives are not.

### 7.5 Static classifications

Use precise states:

```text
MERGE_ELIGIBLE
DIFFERENT_PREDICATE
UNSUPPORTED_MODEL_TYPE
UNSUPPORTED_IMPORT_SHAPE
UNSUPPORTED_COMPARISON_TYPE
PROJECTION_COLLISION
SOURCE_MAPPING_AMBIGUOUS
REFERENCE_BINDING_AMBIGUOUS
NONDETERMINISTIC
```

Human output may explain that different predicates often represent intentional slicing, but the machine state does not infer intent.

## 8. Rewrite planning and source editing

### 8.1 Canonical CTE

The canonical CTE is the first eligible import in source order. This rule is deterministic, easy to explain, and avoids moving an earlier CTE behind a later dependency.

### 8.2 Reference preservation

The planner removes donor CTE declarations and rewrites only table-reference tokens proven to bind to those CTEs.

When no explicit alias exists, preserve the removed CTE name as an alias:

```sql
-- before
from order_financials

-- after
from orders as order_financials
```

When an alias already exists, retain it:

```sql
-- before
from order_financials as f

-- after
from orders as f
```

Downstream qualifiers therefore remain unchanged. No global identifier replacement is needed.

### 8.3 Text-edit rules

The rewrite planner produces immutable, non-overlapping byte edits without touching the real file:

- append missing direct projection fragments to the canonical select list;
- delete complete donor CTE spans with correct comma ownership;
- replace bound donor table-reference spans and add an alias when required;
- retain every byte outside declared edits.

Edits are applied from highest byte offset to lowest.

Comments require an explicit rule. Comments attached to a copied select item move with that item. If deleting a donor CTE would discard any other comment, v1 returns `COMMENT_RELOCATION_UNSUPPORTED` rather than silently losing it.

The candidate is re-lexed, re-parsed, and compiled. A source diff alone is never a valid safety signal.

### 8.4 Compiled-delta gate

The original and candidate are compiled under the same:

- dbt executable and adapter;
- project/profile/target;
- CLI vars and supported flags;
- package state;
- non-secret environment context;
- original model path and identity.

Both compiled ASTs are compared. The delta must consist only of the planned projection union, donor CTE removal, and bound relation redirection. Any other change is `COMPILE_DRIFT`.

Reject models whose semantics depend on destination state or invocation-specific context that cannot be held equivalent, including `is_incremental()`, relevant uses of `this`, volatile SQL functions, sequences, remote functions, or execute-time macros that produce unexplained differences.

## 9. Filesystem workspace

### 9.1 Source snapshot and candidate copy

Verification never edits the user working tree. It creates an OS temporary workspace:

```text
<temp>/dbt-refmerge/<run_id>/
├── source_snapshot/
├── candidate_project/
├── harness_project/
├── artifacts/
│   ├── baseline/
│   ├── candidate/
│   └── harness/
└── run-ledger.json
```

The source snapshot includes the current project state, including relevant untracked files. The candidate is derived from that exact snapshot.

The copy excludes `.git`, logs, targets, virtual environments, and caches, while preserving:

- dbt project files and macros;
- dependency lock/config files;
- already resolved package contents required for identical compilation;
- file mode, encoding, and relative layout;
- local package dependencies, even when located outside the project directory.

Local dependencies are resolved before copying. If their relative layout cannot be reproduced, the command fails closed. Verification must not silently run `dbt deps` and change dependency resolution.

Editable source files are never hard-linked or symlinked to the real working tree. Verified copy-on-write reflinks are allowed. A Git worktree is only an optional optimization for a clean, fully tracked repository; Git is not required for correctness.

### 9.2 Artifact isolation

Every dbt invocation receives unique target and log paths. Existing user artifacts are never overwritten. Partial parsing must not reuse an artifact from a different source/config digest.

Temporary source copies are deleted on normal completion unless `--keep-workspace` is explicitly selected. Persisted reports and receipts are sanitized and stored in an OS-appropriate user cache or a caller-selected path, not automatically inside a dbt model path.

## 10. Verification harness

### 10.1 Minimal project

The harness is a generated dbt project containing:

- a baseline view model whose body is the original compiled `SELECT`;
- a candidate view model whose body is the candidate compiled `SELECT`;
- generated schema/comparison queries;
- a controlled `generate_schema_name` implementation;
- no user project hooks, packages, or resource configuration.

It uses the user-selected profile/target credentials but writes only to the explicitly supplied scratch database/schema.

Already compiled SQL is embedded as literal SQL so the harness does not reinterpret it as user Jinja. The harness relation names are generated identifiers, not user-controlled SQL fragments.

### 10.2 Write preflight

Before any DDL:

1. parse/compile the harness;
2. read every writable node’s fully resolved relation;
3. normalize it with adapter rules;
4. verify it lies inside the exact configured scratch boundary;
5. verify the run identity and collision-free object names;
6. abort if any object escapes the boundary.

Do not use target-name heuristics. Reading production inputs while writing to an isolated scratch area can be legitimate; writing to production under a target named `dev` is not.

### 10.3 Same-input execution

Create the two scratch views, inspect their schemas, then execute one comparison statement that references both views.

```text
same warehouse statement
    |
    +-- baseline view -> original compiled query -> upstream inputs
    |
    +-- candidate view -> candidate compiled query -> same upstream inputs
```

The one statement is the v1 input-isolation mechanism. Separate result-table builds plus before/after fingerprints are not sufficient. A fingerprint can be emitted as diagnostic metadata but cannot authorize a fix.

If a supported platform cannot give the two view expansions one consistent statement snapshot for the actual input types, the result is `INPUT_ISOLATION_UNAVAILABLE`.

External tables, remote services, streaming buffers, and other inputs outside the warehouse snapshot require capability-specific exclusion or an explicitly weaker, non-fixable result.

## 11. Equality specification

### 11.1 Schema equality

Schemas are equal only when they have the same ordered sequence of:

```text
(dialect-aware output identifier, exact normalized warehouse type)
```

Type compatibility is not equality. Schema comparison occurs before constructing the data comparison so set-operation coercion cannot hide a type change.

v1’s schema claim covers output column names, order, and types. Nullability, comments, policy tags, and physical relation metadata are outside the query-result guarantee unless a warehouse strategy explicitly adds them.

### 11.2 Multiset equality

For relations `A` and `B`:

```text
A == B
iff schema(A) == schema(B)
and for every row value r: multiplicity_A(r) == multiplicity_B(r)
```

Report directional counts:

```text
baseline_only_occurrences
candidate_only_occurrences
```

Avoid one ambiguous `difference_count` unless it is explicitly defined as their sum.

### 11.3 Comparator strategies

Strategy selection is capability-based by adapter and output type:

1. use bidirectional `EXCEPT ALL` when the adapter and every output type have certified semantics;
2. otherwise use a signed grouped-count comparison when every output type is groupable with certified equality semantics;
3. otherwise return `UNSUPPORTED_COMPARISON_TYPE`.

Portable grouped-count shape:

```sql
with tagged as (
    select <all output columns>, 0 as <side_column>
    from <baseline_view>

    union all

    select <all output columns>, 1 as <side_column>
    from <candidate_view>
),
grouped as (
    select
        <all output columns>,
        sum(case when <side_column> = 0 then 1 else 0 end) as baseline_n,
        sum(case when <side_column> = 1 then 1 else 0 end) as candidate_n
    from tagged
    group by <all output columns>
)
select
    coalesce(sum(baseline_n), 0) as baseline_rows,
    coalesce(sum(candidate_n), 0) as candidate_rows,
    coalesce(sum(
        case when baseline_n > candidate_n then baseline_n - candidate_n else 0 end
    ), 0) as baseline_only_occurrences,
    coalesce(sum(
        case when candidate_n > baseline_n then candidate_n - baseline_n else 0 end
    ), 0) as candidate_only_occurrences
from grouped
```

The generated side-column identifier is collision-checked and adapter-quoted.

SQL `GROUP BY` groups nulls together, which is correct for bag counting. Adapter conformance tests must still cover nulls, `NaN`, infinities, signed zero, collations, binary values, timestamps, decimals, arrays, structs, variants/JSON, and geography.

### 11.4 Disallowed verdict shortcuts

None of these may produce `SNAPSHOT_EQUIVALENT`:

- row counts alone;
- `EXCEPT`/`MINUS` with distinct semantics;
- sampled rows;
- limited diff results without a full verdict query;
- aggregate hashes or checksums;
- approximate comparisons;
- numeric tolerances;
- serialization of complex values without proven canonical injective encoding.

BigQuery documents `EXCEPT DISTINCT`, not `EXCEPT ALL`, and Redshift documents that `EXCEPT ALL` is unsupported. Current Snowflake set-operator documentation exposes `MINUS`/`EXCEPT` separately from `UNION ALL`. The implementation therefore uses tested capabilities rather than an assumed cross-warehouse matrix. [BigQuery set operators](https://cloud.google.com/bigquery/docs/reference/standard-sql/query-syntax#set_operators), [Redshift set operators](https://docs.aws.amazon.com/redshift/latest/dg/r_UNION.html), and [Snowflake set operators](https://docs.snowflake.com/en/sql-reference/operators-query)

The current `dbt_utils.equality` macro uses distinct `EXCEPT` in both directions, so it cannot serve as the strict bag-equality safety kernel. `dbt-audit-helper` remains useful inspiration and a diagnostic option, but its primary-key-oriented tools are not the required dependency-free exact primitive. [dbt-utils equality implementation](https://github.com/dbt-labs/dbt-utils/blob/main/macros/generic_tests/equality.sql) and [dbt-audit-helper](https://github.com/dbt-labs/dbt-audit-helper)

## 12. Warehouse capability layer

Do not begin with five mostly empty subclasses. Start with one supported warehouse and a declarative capability record plus small strategy functions.

```python
@dataclass(frozen=True)
class VerificationCapabilities:
    adapter_type: str
    sqlglot_dialect: str
    bag_strategy: Literal["except_all", "grouped_counts"]
    supported_exact_types: frozenset[str]
    scratch_relation_type: Literal["view"]
    statement_snapshot_supported: bool
    query_cost_controls: frozenset[str]
```

Platform direction:

| Warehouse | v1 | Later stronger isolation/materialization work |
|---|---|---|
| Snowflake | Scratch views plus one comparison statement; transient scratch container where appropriate | Time Travel or database/schema/table clone at a recorded point. External tables are not cloned, and fully qualified view references can still point outside a clone. [Snowflake cloning](https://docs.snowflake.com/en/sql-reference/sql/create-clone) |
| BigQuery | Scratch dataset views plus one comparison query; labels, expiration, and maximum-bytes controls | Multi-statement snapshot transactions or coordinated table snapshots/clones. External data is not guaranteed consistent in a transaction. [BigQuery transactions](https://cloud.google.com/bigquery/docs/transactions) |
| Databricks | Scratch catalog/schema views plus one SQL statement for certified table types | Pin all Delta inputs to versions/timestamps or use shallow/deep clones, subject to Unity Catalog and table-format rules. [Databricks table history](https://docs.databricks.com/aws/en/tables/history) |
| Redshift | Scratch views plus one comparison statement | A single connection and SNAPSHOT/SERIALIZABLE transaction. Separate dbt subprocesses cannot share it. [Redshift isolation](https://docs.aws.amazon.com/redshift/latest/dg/c_serial_isolation.html) |
| Postgres | Scratch views plus one statement | One connection with `REPEATABLE READ` and optional temporary relations. [PostgreSQL isolation](https://www.postgresql.org/docs/current/transaction-iso.html) |

The first advertised adapter requires real integration tests. DuckDB can exercise the pipeline cheaply but cannot certify another warehouse’s dialect, type, snapshot, or equality behavior.

## 13. Incremental, ephemeral, and special models

### 13.1 Incremental

Incremental models may be scanned and reported but are `UNSUPPORTED_MODEL_TYPE` for automatic v1 rewriting.

Materializing the query as a new full table tests only the full-refresh path. It does not test:

- `is_incremental()` branches;
- the existing destination state;
- `unique_key` behavior;
- merge/update/delete semantics;
- microbatch behavior;
- `incremental_predicates`;
- `on_schema_change`.

A future incremental verifier must clone the current destination into two writable scratch relations, pin every upstream input, execute the original and candidate incremental transitions, and compare the final relations. dbt’s clone command can help on supported platforms, but its fallback pointer view is not a writable destination substitute. [dbt clone](https://docs.getdbt.com/reference/commands/clone)

### 13.2 Ephemeral

An ephemeral model has no standalone relation, so it cannot be a directly verified target in v1. A future mode can verify its effect through materialized children.

An eligible table/view model may still depend on unchanged ephemeral parents; dbt will inline them into its compiled query. Macro-generated ephemeral CTEs are not source-edit targets.

### 13.3 Other exclusions

v1 also excludes:

- Python models;
- recursive models/CTEs;
- snapshots and materialized views as rewrite targets;
- models whose enforced contract cannot be reproduced and compared exactly by the harness;
- volatile or environment-dependent query behavior;
- external inputs without the required snapshot guarantee;
- model SQL that is not safely embeddable as the body of a scratch view.

## 14. State machine and domain model

### 14.1 State machine

```text
DUPLICATE_IMPORT_DETECTED
           |
           v
SOURCE_MAPPING
   |              |
   v              v
MAPPABLE       AMBIGUOUS
   |
   v
STATIC_QUALIFICATION
   |              |
   v              v
MERGE_ELIGIBLE  REPORT_ONLY
   |
   v
PATCH_CANDIDATE
   |
   v
COMPILE_AND_VALIDATE_DELTA
   |              |
   v              v
VALID          COMPILE_DRIFT / ERROR
   |
   v
SCRATCH_PREFLIGHT_AND_COMPARE
   |
   +--> SNAPSHOT_EQUIVALENT --> FIXABLE
   +--> DIFFERENT
   +--> UNVERIFIABLE
   +--> ERROR
```

### 14.2 Core objects

Use frozen dataclasses internally. Use Pydantic only at configuration and serialized JSON boundaries if validation warrants it.

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
class ImportCTE:
    model_unique_id: str
    name: str
    ordinal: int
    upstream_unique_id: str
    predicate_fingerprint: str | None
    projections: tuple[ImportProjection, ...]
    cte_span: SourceSpan
    select_list_span: SourceSpan


@dataclass(frozen=True)
class TextEdit:
    span: SourceSpan
    replacement: bytes
    reason_code: str


@dataclass(frozen=True)
class RewritePlan:
    model_unique_id: str
    source_path: str
    original_source_sha256: str
    canonical_cte: str
    removed_ctes: tuple[str, ...]
    edits: tuple[TextEdit, ...]
    candidate_source_sha256: str


class VerificationStatus(str, Enum):
    NOT_RUN = "not_run"
    SNAPSHOT_EQUIVALENT = "snapshot_equivalent"
    DIFFERENT = "different"
    UNVERIFIABLE = "unverifiable"
    ERROR = "error"


@dataclass(frozen=True)
class EqualityResult:
    schema_equal: bool
    baseline_rows: int
    candidate_rows: int
    baseline_only_occurrences: int
    candidate_only_occurrences: int


@dataclass(frozen=True)
class VerificationReceipt:
    run_id: str
    plan_sha256: str
    original_source_sha256: str
    candidate_source_sha256: str
    original_compiled_sha256: str
    candidate_compiled_sha256: str
    dbt_version: str
    manifest_schema_version: str
    adapter_type: str
    sqlglot_version: str
    comparator_version: str
    compilation_context_sha256: str
    equality: EqualityResult
    status: VerificationStatus
    reason_codes: tuple[str, ...]
```

Do not store `verified` as an independently mutable Boolean. Derive fixability from status, equality invariants, receipt integrity, and current source hash.

## 15. Security, safety, and cost controls

dbt projects are trusted executable inputs. `dbt compile` can execute introspective queries and macros. dbt-refmerge cannot sandbox arbitrary Jinja while reproducing the project’s real behavior.

Required controls:

- require an explicit scratch schema for `check`/`fix` unless a reviewed config supplies one;
- allow an explicit scratch database/project/catalog where the adapter supports it;
- preflight every resolved write relation before DDL;
- use least-privilege credentials where practical;
- generate high-entropy run/object identifiers and quote them through dbt relation rules;
- never embed unchecked identifiers through string formatting;
- tag/label scratch objects and queries with run identity;
- set statement/query timeouts and propagate cancellation;
- provide adapter-specific cost controls, including BigQuery maximum bytes billed and dry-run estimates;
- cap concurrency independently of dbt model threads;
- redact secrets from argv rendering, logs, JSON, errors, and stored artifacts;
- validate symlinks and paths before reading or patching;
- lock the source file and compare its hash immediately before atomic replacement;
- clean only exact ledger objects and retain TTL cleanup for crashes.

A maximum row count is not a reliable cost cap because computing the count can itself scan the relation. Cost policy and semantic comparison completeness are separate concerns. If a cost limit prevents a full exact comparison, status is `UNVERIFIABLE`, never success.

## 16. Package structure

Start with cohesive modules rather than the original approximately 30-file design:

```text
dbt-refmerge/
├── pyproject.toml
├── README.md
├── LICENSE
├── src/
│   └── dbt_refmerge/
│       ├── cli.py
│       ├── config.py
│       ├── domain.py
│       ├── dbt_cli.py
│       ├── artifacts.py
│       ├── source.py
│       ├── semantics.py
│       ├── analyze.py
│       ├── rewrite.py
│       ├── workspace.py
│       ├── verification/
│       │   ├── harness.py
│       │   ├── comparator.py
│       │   └── capabilities.py
│       └── reporting.py
└── tests/
    ├── unit/
    ├── property/
    ├── golden/
    ├── integration/
    ├── warehouse/
    └── fixtures/
        ├── dbt_projects/
        ├── manifests/
        └── compiled_sql/
```

Split by warehouse only when the first real adapter proves the strategy boundary. Do not build a generic plugin registry or rewrite DSL until a second refactoring rule exists.

Recommended dependencies:

- Python 3.11+;
- SQLGlot;
- Typer or Click;
- Rich;
- frozen dataclasses internally;
- Pydantic only for external configuration/results if useful;
- standard-library subprocess, pathlib, tempfile, hashlib, JSON, and filesystem primitives.

## 17. Testing architecture

### 17.1 Unit tests

Cover:

- source lexer and byte spans;
- Jinja masking and brace/whitespace-control cases;
- top-level CTE enumeration and comma ownership;
- manifest/ref/source resolution;
- dialect-aware identifier identity;
- projection maps and collisions;
- predicate AST equality;
- CTE scope and shadowing;
- capability selection and comparator generation;
- receipt/fixability derivation.

### 17.2 Golden tests

Each case contains original source, expected candidate source, and expected edit spans. Assert byte equality, not merely parsed equality.

Include:

- LF/CRLF and missing final newline;
- comments and unsupported relocation;
- quoted identifiers;
- existing downstream aliases;
- unaliased donor references requiring alias preservation;
- multiple duplicate groups in one model;
- nested same-name CTEs;
- Jinja before, between, and after CTEs.

### 17.3 Property and mutation tests

Use property tests to establish:

- edits never change bytes outside declared spans;
- formatting-only variations do not change semantic classification;
- duplicate projection union is deterministic;
- source reordering changes only the documented canonical choice.

Use mutation tests against the analyzer. Removing any reject rule should cause a negative-safety fixture to fail.

### 17.4 Comparator conformance

For every advertised warehouse and supported type, run real tests covering:

- equal empty relations;
- one-sided rows;
- equal counts but different values;
- duplicate multiplicity swaps such as `A,A,B` versus `A,B,B`;
- all-null rows and mixed nulls;
- decimal precision/scale;
- timestamps and time zones;
- binary and collation behavior;
- floats including `NaN`, infinities, and signed zero when floats are supported;
- supported arrays/structs/variants and explicit rejection of unsupported types;
- schema name, order, and exact type changes.

DuckDB/Postgres can provide fast CI coverage, but recorded SQL fixtures are not a substitute for a real integration lane on the advertised adapter.

### 17.5 Operational safety tests

Test:

- scratch-relation escape attempts;
- target/schema names that misleadingly contain `dev` or `prod`;
- cancellation and timeout cleanup;
- stale/corrupt run ledgers;
- concurrent `fix` attempts;
- source changes between check and write;
- local packages outside project root;
- dbt/artifact version mismatches;
- compile-time introspection drift;
- cleanup refusing unowned prefix-matching objects.

## 18. Implementation sequence

### Phase 1 — source-only `scan`

- project discovery;
- dbt executable/version discovery;
- source lexer and CTE spans;
- literal ref/source detection;
- parse-manifest loading;
- source-level grouping and reason-coded reports.

### Phase 2 — compiled qualification

- isolated baseline compilation;
- artifact compatibility validation;
- explicit SQLGlot dialect parsing;
- import allowlist;
- projection mapping;
- exact predicate comparison;
- scope-aware CTE reference binding.

### Phase 3 — candidate generation

- immutable rewrite plan;
- alias-preserving table-reference edits;
- minimal source splice;
- golden/property tests;
- candidate compilation;
- compiled-AST delta validation.

### Phase 4 — one-warehouse verification

- minimal harness project;
- scratch boundary and manifest preflight;
- baseline/candidate views;
- exact schema inspection;
- one-statement bag comparator;
- timeouts, cost controls, ledger, cleanup;
- real warehouse conformance suite.

This is the point at which the product can accurately advertise verified checks.

### Phase 5 — atomic `fix`

- model-level combined-candidate verification;
- hash-bound receipt;
- filesystem lock and source compare-and-swap;
- atomic write;
- stable JSON and CI policy.

### Phase 6 — expansion

- second warehouse after full comparator certification;
- changed-only selection and CI ergonomics;
- optional performance measurement;
- incremental-transition verification using clones/snapshots;
- downstream verification for ephemeral targets;
- second deterministic refactoring rule.

Non-goals for v1 remain symbolic predicate equivalence, wildcard expansion, general lineage, incremental/ephemeral target fixes, multi-model transactional rewrites, LLM calls, and a transformation plugin ecosystem.

## 19. Future refactoring engine

Generalize only the transformation boundary:

```python
class Transformation(Protocol):
    def detect(self, context) -> list[Finding]: ...
    def qualify(self, finding, context) -> Eligibility: ...
    def plan(self, finding, context) -> RewritePlan: ...
    def patch(self, plan, source: bytes) -> bytes: ...
```

The shared engine owns:

```text
source snapshot
  -> compile
  -> transformation qualification
  -> patch
  -> candidate compile
  -> compiled-delta validation
  -> isolated snapshot comparison
  -> verification receipt
  -> atomic apply
```

Duplicate-import consolidation is the only transformation in v1. A second real transformation should determine whether a registry, richer intermediate representation, or more general lineage abstraction is warranted.

## 20. Final product statement

`dbt-refmerge` is a standalone, deterministic developer tool for existing dbt projects.

It:

```text
finds repeated direct imports
  -> resolves them to the same dbt resource
  -> admits only an allowlisted merge shape
  -> creates a minimal source-preserving candidate
  -> compiles and validates the exact generated source
  -> evaluates original and candidate under one observable input snapshot
  -> compares ordered schemas and complete row multiplicities
  -> reports precise safe/different/unverifiable/error states
  -> optionally applies only the exact snapshot-equivalent candidate
```

There are no runtime LLM calls.

The differentiator is not merely duplicate-code detection. It is a reviewable static transformation combined with exact, snapshot-scoped warehouse verification and an atomic source application protocol.

## Primary references

- [dbt compile](https://docs.getdbt.com/reference/commands/compile)
- [dbt parse](https://docs.getdbt.com/reference/commands/parse)
- [dbt programmatic invocations](https://docs.getdbt.com/reference/programmatic-invocations)
- [dbt deferral](https://docs.getdbt.com/reference/node-selection/defer)
- [dbt clone](https://docs.getdbt.com/reference/commands/clone)
- [SQLGlot](https://github.com/tobymao/sqlglot)
- [SQLFluff dbt templater](https://docs.sqlfluff.com/en/stable/configuration/templating/dbt.html)
- [dbt-utils equality implementation](https://github.com/dbt-labs/dbt-utils/blob/main/macros/generic_tests/equality.sql)
- [dbt-audit-helper](https://github.com/dbt-labs/dbt-audit-helper)
- [Snowflake set operators](https://docs.snowflake.com/en/sql-reference/operators-query)
- [Snowflake cloning](https://docs.snowflake.com/en/sql-reference/sql/create-clone)
- [BigQuery set operators](https://cloud.google.com/bigquery/docs/reference/standard-sql/query-syntax#set_operators)
- [BigQuery transactions](https://cloud.google.com/bigquery/docs/transactions)
- [Databricks table history/time travel](https://docs.databricks.com/aws/en/tables/history)
- [Redshift set operators](https://docs.aws.amazon.com/redshift/latest/dg/r_UNION.html)
- [Redshift transaction isolation](https://docs.aws.amazon.com/redshift/latest/dg/c_serial_isolation.html)
- [PostgreSQL transaction isolation](https://www.postgresql.org/docs/current/transaction-iso.html)
