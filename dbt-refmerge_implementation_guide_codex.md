# dbt-refmerge — Complete Implementation Guide

**Architecture:** [dbt-refmerge — Final Architecture](./dbt-refmerge_finalized_codex.md)

**Guide version:** 1.0

**Prepared:** 2026-09-15

**Reference v0.1 adapter:** PostgreSQL

**Intended audience:** maintainers implementing the first production-quality release

## 1. Purpose of this guide

This document translates the final architecture into an implementation plan with concrete module contracts, algorithms, command behavior, generated dbt harness files, test cases, milestone acceptance criteria, and release gates.

It is deliberately prescriptive. When the architecture allows several future strategies, this guide chooses the smallest safe v0.1 path.

The implementation is complete only when this end-to-end statement is true:

> Given an eligible SQL model with duplicate direct import CTEs, `dbt-refmerge check` produces a deterministic candidate, proves that the compiled candidate is exactly the intended semantic transform, compares original and candidate query results as complete multisets within one PostgreSQL statement snapshot, and `fix` atomically writes only those exact verified bytes.

## 2. v0.1 scope

### 2.1 Supported

v0.1 supports:

- Python 3.11 and newer;
- dbt projects invokable through a user-selected `dbt` executable;
- supported dbt manifest schemas explicitly listed in code and tested with fixtures;
- PostgreSQL as the first verification adapter;
- SQL models materialized as `table` or `view`;
- top-level, non-recursive import CTEs;
- one literal `ref()` or `source()` per import CTE;
- unqualified direct-column projections with optional aliases;
- identical absent predicates or exactly equivalent allowlisted predicate ASTs;
- source-preserving byte-span edits;
- complete ordered-schema and multiset comparison;
- one model/file per `fix` invocation;
- multiple eligible duplicate groups in that model, verified as one combined candidate.

### 2.2 Report-only or unsupported

v0.1 reports but does not automatically rewrite:

- incremental, ephemeral, Python, snapshot, and materialized-view targets;
- joins, aggregations, windows, `DISTINCT`, set operations, nested query logic, `QUALIFY`, ordering, limits, sampling, pivots, or lateral constructs inside an import CTE;
- `SELECT *` or affected downstream star expansion;
- expression projections other than a bare column and optional alias;
- macro-generated CTE structure, projections, filters, or downstream references;
- ambiguous quoted/case-folded identifiers;
- comments that would be lost when a donor CTE is removed;
- volatile or unknown-volatility whole-model behavior;
- output types without certified exact PostgreSQL comparison semantics;
- models that cannot be embedded safely as scratch views.

### 2.3 Explicit non-goals

Do not implement these in v0.1:

- symbolic predicate equivalence;
- general column lineage;
- project-wide transactional edits;
- cached verification receipts across independent `fix` invocations;
- incremental-transition verification;
- a plugin architecture for transforms;
- source reformatting;
- performance claims or query-plan comparison;
- runtime LLM calls.

## 3. Engineering invariants

Treat these as assertions, not preferences:

1. No user source is changed by `scan` or `check`.
2. No source is changed by `fix` before the final combined candidate is snapshot-equivalent.
3. The bytes written by `fix` are exactly the bytes whose digest is in the current verification receipt.
4. Compiled SQL offsets are never used as source offsets.
5. Every source edit has a reason code and a validated non-overlapping byte span.
6. Unknown syntax, mapping, volatility, artifact schema, output type, or adapter behavior fails closed.
7. Every warehouse write target is resolved by dbt and preflighted inside the explicit scratch boundary before execution.
8. Equality is never inferred from row counts, distinct set difference, sampling, a hash, or a tolerance.
9. Baseline and candidate content are compared in one SQL statement.
10. Cleanup drops only exact objects recorded for the current run.
11. Stable JSON fields and reason codes are separate from human-facing prose.
12. Deterministic ordering is used for files, CTE groups, projections, edits, output, and tests.

Add tests for each invariant before implementing a second warehouse.

## 4. Repository bootstrap

### 4.1 Initial tree

Create this structure:

```text
dbt-refmerge/
├── pyproject.toml
├── README.md
├── LICENSE
├── CHANGELOG.md
├── .pre-commit-config.yaml
├── src/
│   └── dbt_refmerge/
│       ├── __init__.py
│       ├── cli.py
│       ├── config.py
│       ├── errors.py
│       ├── domain.py
│       ├── dbt_cli.py
│       ├── artifacts.py
│       ├── source.py
│       ├── semantics.py
│       ├── analyze.py
│       ├── rewrite.py
│       ├── workspace.py
│       ├── orchestrator.py
│       ├── reporting.py
│       └── verification/
│           ├── __init__.py
│           ├── capabilities.py
│           ├── harness.py
│           └── comparator.py
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

Do not split these modules further until size or a second implementation proves a real boundary.

### 4.2 Python dependencies

Use:

- `sqlglot` for compiled SQL parsing and scopes;
- `jinja2` for parsing literal `ref()`/`source()` expressions, not for rendering dbt projects;
- `typer` for the CLI;
- `rich` for human output;
- `pydantic` for external configuration and versioned JSON result validation;
- `pyyaml` only for safe, non-rendering reads of simple `dbt_project.yml` fields;
- `platformdirs` for cache/result locations;
- a small cross-platform advisory file-lock package if the standard library cannot meet the supported-platform contract.

Development dependencies:

- `pytest` and `pytest-cov`;
- `hypothesis`;
- `ruff`;
- `mypy`;
- `pre-commit`;
- `dbt-postgres` only in the integration-test environment, never as a runtime dependency of dbt-refmerge.

Resolve current compatible versions when implementation starts, add bounded dependency ranges, and commit `uv.lock`. Do not embed an untested dbt version range in package metadata.

### 4.3 Packaging contract

Use a `src/` layout and expose:

```toml
[project.scripts]
dbt-refmerge = "dbt_refmerge.cli:app"
```

The package must not depend on `dbt-core`, `dbt-adapters`, or `dbt-postgres`. It invokes the user’s dbt command across a subprocess/artifact boundary.

### 4.4 Quality configuration

Initial policy:

- Ruff formatting and linting in CI;
- mypy strict mode for `src/`;
- 100% branch coverage for source editing, fixability derivation, scratch preflight, and comparator-result parsing;
- no blanket `# type: ignore` or lint-disable comments in the safety kernel;
- deterministic tests with frozen fixture data;
- warnings treated as errors in project tests.

## 5. Configuration model

### 5.1 Precedence

Configuration precedence is:

```text
CLI option
  > DBT_REFMERGE_* environment variable
  > .dbt-refmerge.toml in the project root
  > safe built-in default
```

Never read or rewrite `profiles.yml` merely to persist dbt-refmerge settings.

### 5.2 Configuration objects

Implement in `config.py`:

```python
class FailOn(str, Enum):
    NEVER = "never"
    FINDING = "finding"
    FIXABLE = "fixable"
    DIFFERENT = "different"
    UNVERIFIABLE = "unverifiable"


class AppConfig(BaseModel):
    project_dir: Path
    profiles_dir: Path | None
    profile: str | None
    target: str | None
    dbt_command: tuple[str, ...] = ("dbt",)
    scratch_database: str | None = None
    scratch_schema: str | None = None
    subprocess_timeout_seconds: int = 1800
    warehouse_statement_timeout_ms: int = 900_000
    warehouse_lock_timeout_ms: int = 10_000
    max_planner_total_cost: Decimal | None = None
    fail_on: FailOn = FailOn.FIXABLE
    keep_workspace: bool = False
    allow_compile_introspection: bool = False
    json_output: bool = False
    debug: bool = False
```

Validation rules:

- resolve `project_dir` without following an untrusted final symlink;
- require `dbt_project.yml` at the resolved root;
- require `scratch_schema` for `check` and `fix`;
- reject NULs and control characters in external string values;
- place conservative length limits on identifiers and paths before adapter handling;
- keep the dbt command as an argv tuple, never a shell string;
- do not log environment values whose names match secret/password/token/key patterns.

### 5.3 Compilation context

Create one immutable context per command and reuse it for baseline and candidate:

```python
@dataclass(frozen=True)
class CompilationContext:
    dbt_executable: Path
    dbt_version_text: str
    project_dir: Path
    profiles_dir: Path | None
    profile: str
    target: str | None
    vars_json: str | None
    passthrough_args: tuple[str, ...]
    environment_names: tuple[str, ...]
    package_state_sha256: str
```

Pass the same in-memory environment mapping to both compilation subprocesses. Do not persist secret values in the context digest. Cached cross-command receipts are out of v0.1, so a secret-value digest is unnecessary.

### 5.4 CLI contract and exit codes

Expose these v0.1 commands:

```text
dbt-refmerge scan [SELECTION OPTIONS]
dbt-refmerge check [SELECTION OPTIONS] --scratch-schema <schema>
dbt-refmerge fix <model-path> --scratch-schema <schema> [--dry-run]
dbt-refmerge cleanup --run-id <run-id>
```

Common options:

- `--project-dir`, defaulting to the current directory;
- `--profiles-dir`, `--profile`, and `--target`, passed through as validated argv values;
- repeatable `--dbt-command-part`, or an equivalent typed mechanism, for wrapper commands without invoking a shell;
- `--select` for dbt selection syntax on `scan` and `check`;
- `--json`, which writes exactly one versioned JSON document to stdout and sends operational logs to stderr;
- `--fail-on`, using `FailOn` values to control policy findings without changing operational-error meanings;
- `--command-timeout`, `--statement-timeout`, and `--lock-timeout`, all validated as positive bounded durations;
- optional `--max-planner-cost` on PostgreSQL, clearly labeled as an estimate rather than a hard spend limit;
- `--debug` and `--keep-workspace`, with secret redaction still enforced.

Command-specific behavior:

- `scan` runs `dbt parse` when a fresh compatible parse artifact is unavailable. It never silently upgrades itself to `compile`; `--compile` is explicit.
- `check` requires at least one exact selected SQL model and a scratch boundary. It may write only the reported scratch objects and never source.
- `fix` accepts one project-relative model path in v0.1, always reruns the complete `check` pipeline, and writes only after a new receipt is fixable. `--dry-run` performs all verification but writes nothing.
- `cleanup` requires a valid locally stored run ledger or an explicit exact relation ledger. It never discovers deletion targets with a wildcard or schema-name prefix alone.

Reserve stable process exit codes:

```text
0    command completed and configured policy did not fail
1    configuration, dbt, warehouse, cleanup, or internal error
2    static finding/fixable finding triggered --fail-on policy
3    executed candidate was different
4    candidate was unverified or unsupported when policy requires verification
130  interrupted by the user
```

Multiple result categories use the highest-severity applicable nonzero code according to an explicit table in `reporting.py`; never derive precedence from enum ordering. The JSON document records all model-level outcomes regardless of the aggregate exit code.

## 6. Domain model and reason codes

### 6.1 Core dataclasses

Implement frozen dataclasses in `domain.py`:

```python
@dataclass(frozen=True, order=True)
class IdentifierIdentity:
    # Adapter-normalized resolved spelling; quoting has already been applied.
    value: str


@dataclass(frozen=True)
class Identifier:
    source_text: str
    value: str
    quoted: bool
    identity: IdentifierIdentity


@dataclass(frozen=True, order=True)
class SourceSpan:
    start_byte: int
    end_byte: int

    def validate(self, file_size: int) -> None: ...


@dataclass(frozen=True)
class JinjaSpan:
    span: SourceSpan
    kind: Literal["expression", "statement", "comment", "raw"]
    text: bytes


@dataclass(frozen=True)
class RefCall:
    kind: Literal["ref", "source"]
    package: str | None
    name: str
    source_name: str | None
    version: str | int | None
    span: SourceSpan


@dataclass(frozen=True)
class Projection:
    upstream_identifier: Identifier
    output_identifier: Identifier
    span: SourceSpan
    attached_comment_spans: tuple[SourceSpan, ...]


@dataclass(frozen=True)
class SourceCTE:
    identifier: Identifier
    ordinal: int
    cte_span: SourceSpan
    body_span: SourceSpan
    select_list_span: SourceSpan
    separator_span: SourceSpan | None
    ref_call: RefCall | None
    projections: tuple[Projection, ...]
    predicate_source_span: SourceSpan | None
```

`Identifier` stores original text, quoted status, and adapter-normalized identity separately. Never use one lowercased string for all three.

Semantic and planning objects:

```python
@dataclass(frozen=True)
class SemanticProjection:
    upstream_identity: IdentifierIdentity
    output_identity: IdentifierIdentity
    ast_path: tuple[int | str, ...]


@dataclass(frozen=True)
class SemanticImport:
    cte_identity: IdentifierIdentity
    upstream_unique_id: str
    projections: tuple[SemanticProjection, ...]
    predicate_fingerprint: str | None
    ast_path: tuple[int | str, ...]


@dataclass(frozen=True)
class TextEdit:
    span: SourceSpan
    replacement: bytes
    reason_code: ReasonCode


@dataclass(frozen=True)
class RewritePlan:
    model_unique_id: str
    source_path: Path
    original_source_sha256: str
    canonical_cte: Identifier
    removed_ctes: tuple[Identifier, ...]
    edits: tuple[TextEdit, ...]
    candidate_source_sha256: str


@dataclass(frozen=True, order=True)
class RelationIdentity:
    database: IdentifierIdentity
    schema: IdentifierIdentity
    identifier: IdentifierIdentity


@dataclass(frozen=True)
class ScratchBoundary:
    database: IdentifierIdentity
    schema: IdentifierIdentity
    allowed_relations: tuple[RelationIdentity, ...]
```

### 6.2 Statuses

```python
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
    model_unique_id: str
    source_path: Path
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
    scratch_relations: tuple[RelationIdentity, ...]
    equality: EqualityResult
    status: VerificationStatus
    reason_codes: tuple[ReasonCode, ...]
    warning_codes: tuple[str, ...]
    cleanup_complete: bool
```

`verified` is always derived:

```python
def is_fixable(receipt: VerificationReceipt) -> bool:
    return (
        receipt.status is VerificationStatus.SNAPSHOT_EQUIVALENT
        and receipt.reason_codes == (ReasonCode.OK,)
        and receipt.cleanup_complete
        and receipt.equality.schema_equal
        and receipt.equality.baseline_rows == receipt.equality.candidate_rows
        and receipt.equality.baseline_only_occurrences == 0
        and receipt.equality.candidate_only_occurrences == 0
    )
```

`reason_codes` contains blocking outcome reasons and is exactly `(OK,)` on success. Non-blocking disclosures, such as a user-declared deterministic UDF allowlist entry, belong in `warning_codes`. Cleanup failure does not rewrite a correct equality result to `DIFFERENT`, but it blocks source application until cleanup succeeds and verification is rerun.

### 6.3 Reason-code taxonomy

Define a stable enum early. Minimum v0.1 codes:

```text
OK
NO_DUPLICATE_IMPORT
NEEDS_COMPILED_ANALYSIS
UNSUPPORTED_MANIFEST_SCHEMA
UNSUPPORTED_ADAPTER
UNSUPPORTED_MODEL_TYPE
UNSUPPORTED_IMPORT_SHAPE
UNSUPPORTED_COMPARISON_TYPE
DIFFERENT_PREDICATE
PROJECTION_COLLISION
SOURCE_MAPPING_AMBIGUOUS
REFERENCE_BINDING_AMBIGUOUS
COMMENT_RELOCATION_UNSUPPORTED
NONDETERMINISTIC
COMPILE_REQUIRES_INTROSPECTION
COMPILE_DRIFT
HARNESS_EMBEDDING_UNSAFE
SCRATCH_BOUNDARY_VIOLATION
INPUT_ISOLATION_UNAVAILABLE
SCHEMA_MISMATCH
BAG_DIFFERENCE
SOURCE_CHANGED_DURING_SNAPSHOT
SOURCE_CHANGED_BEFORE_APPLY
DBT_COMMAND_FAILED
WAREHOUSE_TIMEOUT
RESOURCE_LIMIT
CLEANUP_FAILED
INTERNAL_ERROR
```

Never reuse a reason code with a changed meaning after release. Add codes instead.

## 7. dbt subprocess implementation

### 7.1 Process wrapper

`dbt_cli.py` owns all subprocess execution:

```python
@dataclass(frozen=True)
class CommandResult:
    argv_redacted: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool


class DbtCli:
    def version(self) -> DbtVersion: ...
    def parse(self, invocation: DbtInvocation) -> CommandResult: ...
    def compile(self, invocation: DbtInvocation, selector: str) -> CommandResult: ...
    def run(self, invocation: DbtInvocation, selector: str) -> CommandResult: ...
    def run_operation(
        self,
        invocation: DbtInvocation,
        macro_name: str,
        args: Mapping[str, JSONValue] | None = None,
    ) -> CommandResult: ...
```

Implementation rules:

- use `subprocess.Popen`/`run` with `shell=False`;
- pass an argv list;
- pass an explicit working directory and environment;
- capture stdout/stderr separately;
- send termination on timeout, then kill after a short grace period;
- propagate Ctrl-C to the child process group;
- cap retained log bytes while writing the complete redacted log to the run workspace;
- raise typed operational errors containing a reason code and redacted command;
- never include profile contents or raw environment values in exceptions.

### 7.2 Capability discovery

dbt v1 and newer engines may expose flags differently. At startup:

1. run `<dbt-command> --version`;
2. run `<dbt-command> --help` and, when needed, `compile --help`;
3. build a `DbtCliCapabilities` object;
4. fail if required flags/artifacts are unavailable.

Do not infer support only from a parsed semantic version. Wrapper scripts and alternate engines can report differently.

### 7.3 Deterministic invocation

Use unique target and log paths for every command. Prefer a full parse/compile rather than partial-parse cache reuse in the temporary project. Where supported, the baseline compile command is conceptually:

```text
dbt
  --no-populate-cache
  compile
  --no-introspect
  --project-dir <snapshot>
  --profiles-dir <profiles-dir>
  --profile <profile>
  --target <target>
  --target-path <unique-baseline-target>
  --select <exact-model-selector>
  --no-partial-parse
```

Actual flag placement comes from `DbtCliCapabilities` and integration tests.

Default to no cache population and no introspection when the installed dbt supports those controls. If compilation requires introspection:

- `scan --compile` reports `COMPILE_REQUIRES_INTROSPECTION`;
- `check`/`fix` fail closed unless `--allow-compile-introspection` is explicitly set;
- baseline and candidate still use the same flag and environment;
- compiled-delta validation remains mandatory.

dbt documents that compilation can query the warehouse through cache population and `run_query`, while parsing alone does not. [dbt compile](https://docs.getdbt.com/reference/commands/compile)

### 7.4 Exact node selection

Do not assume a bare model name is unique. Build a selector from the manifest and source path, invoke dbt listing/selection where necessary, and assert that the resulting manifest has exactly one selected model with the expected:

- `unique_id`;
- `original_file_path`;
- `resource_type`;
- package/project identity.

Selection mismatch is an operational error, not an empty successful check.

## 8. Artifact loading

### 8.1 Narrow schema adapter

`artifacts.py` loads only fields needed by the tool:

```python
class ManifestMetadataModel(BaseModel):
    dbt_schema_version: str
    dbt_version: str
    invocation_id: str | None = None
    adapter_type: str | None = None
    project_name: str | None = None

    model_config = ConfigDict(extra="allow")


class ManifestNodeModel(BaseModel):
    unique_id: str
    resource_type: str
    package_name: str
    name: str
    original_file_path: str
    relation_name: str | None = None
    raw_code: str = ""
    compiled_code: str | None = None
    depends_on: DependsOnModel
    refs: list[RefArgsModel] = Field(default_factory=list)
    sources: list[list[str]] = Field(default_factory=list)
    config: dict[str, Any]

    model_config = ConfigDict(extra="allow")
```

Create a per-manifest-version field adapter rather than scattering `dict.get()` calls through the analyzer.

The official dbt JSON schema catalog currently exposes multiple manifest versions; validate the artifact URI/version before accessing fields. [dbt JSON schemas](https://schemas.getdbt.com/)

### 8.2 Loader checks

The loader must:

- reject artifacts above a configured size limit before reading them wholly;
- require valid UTF-8 JSON;
- validate metadata first;
- allow unknown additive fields but reject missing required safety fields;
- reject duplicate dictionary keys if the JSON parser can be configured to detect them;
- ensure all model paths resolve inside an approved snapshot root;
- distinguish `compiled_code is None` from empty compiled SQL;
- normalize path separators without normalizing identifier case;
- preserve original artifact files for diagnostics when requested.

### 8.3 Ref/source resolution

Resolve a literal source call without reimplementing dbt’s full resolver:

1. parse the call’s constant arguments from source;
2. take dependency candidates from the owning node’s manifest dependencies;
3. filter candidates by resource kind, package/source name, model/table name, and version when present;
4. cross-check manifest `refs`/`sources` metadata when that artifact version provides it;
5. require exactly one candidate unique id.

Zero or multiple candidates produce `SOURCE_MAPPING_AMBIGUOUS`.

## 9. Workspace implementation

### 9.1 Context manager

`workspace.py` provides:

```python
class RunWorkspace(AbstractContextManager["RunWorkspace"]):
    run_id: str
    root: Path
    source_snapshot: Path
    candidate_project: Path
    harness_project: Path
    artifacts_root: Path
    ledger_path: Path

    @classmethod
    def create(cls, config: AppConfig) -> "RunWorkspace": ...
    def snapshot_project(self) -> SnapshotResult: ...
    def create_candidate(self) -> None: ...
    def record_object(self, obj: ScratchObject) -> None: ...
    def cleanup_files(self) -> None: ...
```

Use `tempfile.mkdtemp()` or its safe equivalent. A run id contains a time-sortable component plus strong random entropy, but is still validated before use in identifiers.

### 9.2 Stable project snapshot

Snapshot algorithm:

1. discover the project root by locating the selected `dbt_project.yml`;
2. enumerate files without following arbitrary symlinks;
3. identify configured runtime directories such as target/log/cache paths;
4. identify local package dependencies and their allowed roots;
5. record path, type, size, mode, modification time, and content hash for included files;
6. copy to `source_snapshot`;
7. re-stat and re-hash source files that changed metadata during copying;
8. fail with `SOURCE_CHANGED_DURING_SNAPSHOT` if a stable snapshot cannot be obtained;
9. create `candidate_project` from the stable snapshot;
10. verify the candidate tree hash before patching.

Do not rely on `.gitignore` as an inclusion policy; ignored files can still be required by dbt. Exclude only known runtime/output locations and configured limits.

Symlink policy:

- reject a symlink whose resolved target is outside approved project/local-package roots;
- copy safe file contents into the snapshot rather than leave an editable link to the source;
- reject symlink cycles and special files;
- never copy sockets, devices, or FIFOs.

### 9.3 Package-state digest

Hash the files that determine package behavior:

- dependency declarations and lock files;
- macro files in resolved packages;
- local package project/config/macro files;
- the root project macro files.

Baseline and candidate must have the same package-state digest. Do not run `dbt deps` automatically during verification.

## 10. Source frontend

`source.py` is part of the safety kernel. Keep it small, deterministic, and heavily tested.

### 10.1 Decode and offset mapping

Read bytes first:

1. preserve an optional UTF-8 BOM;
2. decode strict UTF-8;
3. reject invalid encoding in v0.1;
4. detect LF, CRLF, and mixed newline use;
5. construct a character-index-to-byte-offset table in one pass;
6. retain original bytes for all edits.

Tokens may use character offsets internally, but persisted source spans are byte offsets.

### 10.2 Jinja scanner

Scan Jinja before SQL because Jinja delimiters are meaningful even where SQL would otherwise see string text.

Recognize:

- `{{ ... }}` and whitespace-control variants;
- `{% ... %}` and whitespace-control variants;
- `{# ... #}`;
- quoted strings and escapes inside Jinja expressions/statements;
- `{% raw %}`/`{% endraw %}` blocks.

For each Jinja span:

- preserve newlines;
- replace non-newline bytes with spaces in the masked source;
- for an eligible literal `ref()`/`source()` expression, insert a short valid sentinel identifier and pad the rest;
- record a bidirectional mapping from sentinel to `RefCall` and source span.

Any unterminated block is a source parse error.

### 10.3 Literal call parser

Parse the contents of `{{ ... }}` with Jinja’s AST. Accept only these shapes inside an eligible import `FROM`:

```jinja
{{ ref('model_name') }}
{{ ref('package_name', 'model_name') }}
{{ ref('model_name', version=1) }}
{{ source('source_name', 'table_name') }}
```

Support dbt’s tested version-keyword spellings through an explicit compatibility table. Every argument must be a constant string/integer. Reject concatenation, variables, `builtins.ref`, macro wrappers, conditionals, filters, and attribute calls in v0.1.

### 10.4 Top-level CTE splitter

Tokenize the masked SQL with source offsets. The splitter must:

1. skip leading whitespace, comments, and model-level config spans;
2. find one top-level `WITH`;
3. reject `WITH RECURSIVE`;
4. read a quoted or unquoted CTE identifier;
5. reject a CTE column-name list in v0.1;
6. require `AS (` with no materialization hint;
7. find the matching close parenthesis with depth tracking;
8. record following separator comma and trivia ownership;
9. repeat until the main query begins;
10. reject duplicate normalized CTE identifiers in one scope.

Define separator ownership in one place. A recommended representation is:

```text
leading trivia | CTE declaration/body | trailing trivia | optional comma
```

Deletion must never consume trivia/comments owned by an adjacent retained CTE.

### 10.5 Import-body parser

For each source CTE body:

- require one `SELECT` at body depth zero;
- find the select-list and `FROM` spans;
- split projections only on depth-zero commas;
- require one sentinel relation as the sole `FROM` item;
- locate an optional `WHERE` and reject every other clause;
- parse each projection as a bare unqualified identifier with optional alias;
- record attached inline/leading comments;
- reject wildcard, qualified column, expression, literal, function, or duplicate output name.

Use SQLGlot against the masked body as a second validation, but let the source tokenizer own byte spans.

### 10.6 Downstream table-reference spans

Parse the full masked source and build scopes. For every donor CTE:

- locate table nodes whose normalized identity matches the donor;
- ensure the scope resolver binds them to the intended top-level CTE rather than a nested shadow;
- map the table identifier token to a source byte span;
- record whether an explicit alias already exists;
- reject mappings that are not one-to-one.

Never regex-replace identifier text.

## 11. Compiled semantic analysis

### 11.1 Dialect parsing

`semantics.py` exposes:

```python
class SemanticAnalyzer:
    def parse_model(self, sql: str, dialect: str) -> ParsedModel: ...
    def match_source_ctes(
        self,
        source_ctes: tuple[SourceCTE, ...],
        parsed: ParsedModel,
        manifest: ManifestView,
    ) -> tuple[MatchedCTE, ...]: ...
    def qualify_import(self, matched: MatchedCTE) -> Qualification: ...
    def analyze_volatility(self, parsed: ParsedModel) -> VolatilityResult: ...
```

Parse with an error level that surfaces unsupported/fallback behavior. Reject multiple statements, DDL/DML, commands, or a root that is not one query expression embeddable in a view.

### 11.2 Source-to-compiled match

For each source CTE:

1. normalize its identifier using adapter rules;
2. find a top-level compiled CTE with the same identity;
3. confirm that the compiled CTE’s sole input relation maps to the expected manifest dependency;
4. compare projection output/upstream identities;
5. compare predicate presence;
6. use ordinal only as a consistency check among matched source-authored CTEs, not as an absolute compiled ordinal because dbt may inject ephemeral CTEs;
7. require exactly one semantic match.

Any injected/macro-generated compiled CTE that has no source counterpart is not editable and cannot be selected as a donor/canonical CTE.

### 11.3 Import allowlist visitor

Implement a positive AST validator. It should enumerate allowed node types and reject everything else.

For a PostgreSQL import CTE, expected nodes are approximately:

- `Select`;
- direct `Column`/`Identifier` projections and optional `Alias`;
- one `From` containing one `Table`;
- optional `Where`;
- predicate nodes from the explicit Boolean/comparison allowlist;
- literal nodes for supported scalar literal types.

Do not rely only on checks such as “no join found.” The visitor returns all unexpected AST classes so reports and tests are useful.

### 11.4 Predicate fingerprint

Create a stable structural fingerprint:

1. resolve/qualify predicate column bindings within the single relation scope;
2. strip source metadata and comments;
3. normalize dialect-defined unquoted identifier case;
4. serialize the AST into a project-owned canonical tuple/JSON form;
5. hash the serialized bytes with SHA-256.

Do not use Python’s process-randomized `hash()`.

The canonical serializer is versioned. A SQLGlot upgrade requires predicate and compiled-AST fixture review.

### 11.5 Whole-model volatility gate

Analyze the entire compiled query, not only import CTEs. Maintain an adapter registry of:

- known volatile functions (`random`, UUID generators, sequence access, clock functions with non-statement stability, and adapter equivalents);
- order-sensitive functions/aggregates without deterministic ordering;
- sampling constructs;
- unordered `LIMIT`/`FETCH`;
- remote/external functions;
- unknown UDFs.

Policy:

- known volatile construct: `NONDETERMINISTIC`;
- unknown function volatility: unfixable unless the user config explicitly allowlists the fully qualified function as deterministic;
- an allowlist entry changes eligibility only and is recorded in the receipt/report;
- `run_started_at`/`invocation_id`-driven compiled literals will normally be caught by compiled-delta validation, but source/macro usage should also be reported when detectable.

Be conservative with `row_number`, `first_value`, unordered array/string aggregation, and similar tie/order behavior. A refactor can change the execution plan even when relational inputs are the same.

## 12. Analysis orchestration

`analyze.py` combines source, manifest, and semantic information without creating edits.

### 12.1 Static scan result

```python
@dataclass(frozen=True)
class Finding:
    model_unique_id: str
    source_path: Path
    upstream_unique_id: str
    cte_names: tuple[str, ...]
    status: FindingStatus
    reason_codes: tuple[ReasonCode, ...]
```

Sort findings by normalized source path, model unique id, upstream unique id, then first CTE ordinal.

### 12.2 Duplicate-group algorithm

```python
def group_imports(imports: Iterable[MatchedImport]) -> tuple[DuplicateGroup, ...]:
    grouped: dict[tuple[str, str], list[MatchedImport]] = defaultdict(list)
    for item in imports:
        grouped[(item.model_unique_id, item.upstream_unique_id)].append(item)

    result = []
    for key in sorted(grouped):
        members = sorted(grouped[key], key=lambda item: item.source_cte.ordinal)
        if len(members) >= 2:
            result.append(DuplicateGroup(..., imports=tuple(members)))
    return tuple(result)
```

### 12.3 Group qualification

A duplicate group is `MERGE_ELIGIBLE` only if:

- all members independently satisfy the import allowlist;
- all resolve to the same upstream unique id;
- all predicate fingerprints match, including all absent;
- their projection union has no output collision;
- every source/compiled mapping is unique;
- every donor downstream reference is safely bound and editable;
- no affected CTE star is observable;
- comment handling can preserve all comments;
- the containing compiled model passes whole-model volatility checks;
- the target and output types are within the supported model/comparison scope.

Return all applicable reason codes, not only the first, except when continuing would be unsafe or misleading.

## 13. Rewrite planner and patcher

### 13.1 Pure planner

`rewrite.py` must be pure for a fixed source byte string and qualified findings:

```python
def build_plan(
    source: bytes,
    model: QualifiedModel,
    groups: tuple[QualifiedDuplicateGroup, ...],
) -> RewritePlan:
    ...
```

The planner never reads the live source path and never writes a file.

### 13.2 Canonical and projection ordering

For each group:

- canonical CTE: first source ordinal;
- retain canonical projection order;
- append missing output identifiers in first-seen donor/source order;
- deduplicate only when both output and upstream identifier identities match;
- reject one output identity mapped to different upstream identities.

### 13.3 Projection insertion

Infer indentation and comma layout only from the canonical select list:

- multiline list: preserve its newline sequence and indentation;
- single-line list: v0.1 may reject rather than reflow it, unless a tested minimal insertion is unambiguous;
- trailing commas: preserve the existing style;
- copied alias spelling/quoting comes from the donor source fragment;
- attached comments move only with their projection fragment.

Do not run a formatter.

### 13.4 Donor deletion

Compute deletion spans through the CTE splitter’s separator ownership. Before accepting a deletion:

- ensure no unclaimed comment lies inside the deletion span;
- ensure it does not overlap the canonical insertion or a reference edit;
- simulate the edit and confirm the remaining top-level `WITH` list tokenizes correctly.

### 13.5 Reference redirection

For a donor reference:

```sql
from donor
```

replace with:

```sql
from canonical as donor
```

For:

```sql
from donor as d
```

replace only `donor` with `canonical`.

Apply equivalent logic to joined table references only when the downstream join itself is outside the import CTE and its table binding is unambiguous. The import CTE allowlist forbids joins inside imports; it does not require the entire model to be join-free.

### 13.6 Edit validation

Before application:

```python
def validate_edits(edits: tuple[TextEdit, ...], source_size: int) -> None:
    ordered = sorted(edits, key=lambda edit: edit.span)
    previous_end = 0
    for edit in ordered:
        edit.span.validate(source_size)
        if edit.span.start_byte < previous_end:
            raise InternalInvariantError("overlapping source edits")
        previous_end = edit.span.end_byte
```

Apply from the end:

```python
def apply_edits(source: bytes, edits: tuple[TextEdit, ...]) -> bytes:
    result = source
    for edit in sorted(edits, key=lambda item: item.span.start_byte, reverse=True):
        result = result[: edit.span.start_byte] + edit.replacement + result[edit.span.end_byte :]
    return result
```

Then re-run source lexing, CTE parsing, and group detection. The transformed duplicate group must no longer contain multiple upstream imports.

## 14. Candidate compilation and compiled-delta validation

### 14.1 Candidate compile

Write candidate bytes only into `candidate_project`, preserving the original relative path. Compile baseline and candidate with separate target/log directories but the same `CompilationContext`.

Extract the selected node’s `compiled_code` from each manifest and require:

- one embeddable query statement;
- no terminal second statement;
- no unsafe harness raw-block terminator;
- no unresolved Jinja;
- expected adapter and node identity.

Strip only a single syntactic terminal semicolon when creating a view body. Never trim or split SQL through a raw string heuristic.

### 14.2 Expected semantic transform

Do not merely assert that the two compiled SQL strings differ “near the CTE.” Build an expected AST:

1. deep-copy the baseline compiled AST;
2. apply the qualified semantic merge plan to that copy;
3. add missing canonical projection nodes;
4. remove donor CTE nodes;
5. redirect only table nodes bound to donors and preserve their old identity as aliases when needed;
6. produce a versioned semantic fingerprint of the whole expected AST;
7. fingerprint the actual candidate compiled AST;
8. require exact equality.

Comments and source-position metadata are excluded from the semantic fingerprint. Identifiers, quoting, aliases, literals, clause order, types/casts, and every semantic AST argument are included.

If equality fails, return `COMPILE_DRIFT` and store a sanitized AST/SQL diagnostic diff when requested. Never execute that candidate.

### 14.3 Important tests

The compiled-delta gate must catch:

- a macro rendering differently between invocations;
- a changed relation target;
- a misplaced projection insertion;
- an accidentally removed filter;
- a table reference rebound to a nested CTE;
- a Jinja whitespace-control edit changing SQL tokens;
- an invocation timestamp literal;
- a package/config drift between copies.

## 15. PostgreSQL verification capability

### 15.1 Capability record

Begin with:

```python
POSTGRES_CAPABILITIES = VerificationCapabilities(
    adapter_type="postgres",
    sqlglot_dialect="postgres",
    bag_strategy="except_all",
    scratch_relation_type="view",
    statement_snapshot_supported=True,
    # Populate only after conformance tests.
    supported_exact_types=frozenset({...}),
    query_cost_controls=frozenset({"statement_timeout"}),
)
```

Even though PostgreSQL supports `EXCEPT ALL`, implement grouped counts too and run both strategies against the same conformance datasets. This provides a portable second strategy before another warehouse is added.

### 15.2 Initial exact type set

Start smaller than PostgreSQL’s full type system:

- boolean;
- smallint, integer, bigint;
- numeric/decimal with exact precision and scale reporting;
- text and character/varchar with exact schema comparison;
- date;
- timestamp without time zone;
- timestamp with time zone;
- UUID if `EXCEPT ALL` conformance passes;
- bytea if conformance passes.

Initially mark these unsupported until dedicated tests define equality:

- real/double precision and special values;
- JSON/JSONB;
- arrays and composite types;
- ranges and multiranges;
- geometric, network, XML, money, interval, enum, domain, and user-defined types;
- collated text whose collation metadata cannot be compared exactly.

Expand one type family at a time.

### 15.3 Snapshot semantics

PostgreSQL gives one `SELECT` statement one MVCC snapshot under normal `READ COMMITTED` behavior; `REPEATABLE READ` is needed only when multiple statements must share a snapshot. v0.1’s content verdict is one statement over both views. [PostgreSQL transaction isolation](https://www.postgresql.org/docs/current/transaction-iso.html)

Schema inspection and view creation occur separately, but they do not produce the content verdict. A concurrent DDL change that invalidates a view should fail the comparison rather than produce equality.

### 15.4 Warehouse safety and resource controls

Do not decide safety from target names such as `dev` or `prod`. Enforce a resolved relation boundary:

1. require the user to supply the scratch schema explicitly or through reviewed project config;
2. normalize scratch database/schema identifiers with PostgreSQL rules, while retaining their quoted form;
3. reject `pg_catalog`, `information_schema`, temporary/internal schemas, and a scratch schema equal to the selected model's resolved output schema;
4. compile the harness before DDL and require every enabled writable node to resolve to the exact scratch database/schema and current-run alias allowlist;
5. fail if unexpected hooks, seeds, snapshots, tests, or writable resources are enabled;
6. use a dedicated least-privilege role in documentation and CI: read access to inputs plus create/drop access only in the scratch schema;
7. set a distinctive PostgreSQL `application_name`/dbt query comment containing the run id;
8. apply a server-side `statement_timeout` to view creation, schema inspection, comparison, diagnostics, and cleanup, in addition to the client subprocess timeout;
9. apply `lock_timeout` so verification does not wait indefinitely on DDL locks;
10. force one model thread and a sequential verification queue in v0.1;
11. persist the exact relation ledger before the first write and update object state durably after every DDL action;
12. attempt bounded exact-object cleanup on normal failure, timeout, and interruption.

PostgreSQL has no BigQuery-style maximum-bytes-billed control. An optional `EXPLAIN (FORMAT JSON)` budget may reject a plan whose estimated total cost exceeds a configured threshold, but it is only a planning guard and must be labeled as an estimate. The server-side statement timeout is the mandatory runtime bound.

Do not offer `--max-verify-rows` as a cost guarantee: obtaining an exact count can itself scan all input, and an estimate cannot authorize equality. If any resource control stops the complete comparison, return `UNVERIFIABLE`, never `SNAPSHOT_EQUIVALENT`.

## 16. Generated verification harness

### 16.1 Harness files

Generate:

```text
harness_project/
├── dbt_project.yml
├── models/
│   ├── baseline.sql
│   └── candidate.sql
└── macros/
    ├── generate_schema_name.sql
    ├── emit_schema.sql
    ├── compare_relations.sql
    └── cleanup_relations.sql
```

The harness has no packages and no hooks.

### 16.2 `dbt_project.yml`

Conceptual template:

```yaml
name: dbt_refmerge_harness
version: 1.0.0
config-version: 2
profile: "<validated profile name>"

model-paths: ["models"]
macro-paths: ["macros"]
target-path: "target"
clean-targets: ["target"]

models:
  dbt_refmerge_harness:
    +materialized: view
```

If the original project profile field cannot be read as a plain safe scalar, require `--profile`.

### 16.3 Scratch schema macro

The tool-controlled macro returns the validated scratch schema for every harness node. If a scratch database is supplied, use the appropriate database-name generation/config path for the adapter.

Never import the user project’s `generate_schema_name`, because its purpose and output are outside the harness boundary.

### 16.4 View models

Conceptual baseline file:

```jinja
{{ config(alias='<generated_baseline_alias>', materialized='view') }}

{% raw %}
<original compiled query without one terminal semicolon>
{% endraw %}
```

Candidate is identical except for alias and compiled body.

Reject compiled SQL containing a raw-block terminator that prevents safe embedding. The file names and dbt node names are fixed tool constants; only relation aliases carry a validated generated run token.

### 16.5 Harness preflight

Before `dbt run`:

1. compile/parse the harness into its own target path;
2. load its manifest;
3. locate exactly `model.dbt_refmerge_harness.baseline` and `.candidate`;
4. compare each node’s `database`, `schema`, and `alias` fields against `ScratchBoundary` using PostgreSQL identifier rules;
5. require database/schema equality and aliases from the current run’s allowlist;
6. reject any pre/post hook or extra enabled writable node;
7. write the exact relations to `run-ledger.json`;
8. only then run the two view nodes with one thread.

Do not parse a rendered `relation_name` string when structured manifest fields are available.

### 16.6 Schema probe macro

Use a tool-owned `run-operation` macro and `adapter.get_columns_in_relation` to establish portable names/order and generate correctly quoted identifiers. For PostgreSQL, augment it with a tool-owned `pg_catalog` query over the exact view OID to retrieve:

- `attnum` and `attname`;
- `atttypid`, `atttypmod`, and `format_type(...)`;
- `pg_type.typtype` and the defining type namespace;
- collation OID/name and `pg_collation.collisdeterministic` where supported.

Reject domains, enums, user-defined types, dropped/system columns, and nondeterministic or unresolvable collations in v0.1. Cross-check the catalog result against `adapter.get_columns_in_relation`; disagreement is `SCHEMA_MISMATCH` or an adapter error, never a permissive fallback.

Emit:

```json
{
  "baseline": [
    {"ordinal": 1, "name": "order_id", "data_type": "integer"}
  ],
  "candidate": [
    {"ordinal": 1, "name": "order_id", "data_type": "integer"}
  ]
}
```

Print one payload between high-entropy fixed markers:

```text
DBT_REFMERGE_RESULT_<nonce>_BEGIN
<single-line JSON>
DBT_REFMERGE_RESULT_<nonce>_END
```

The parser requires exactly one valid payload with the expected nonce and Pydantic schema. Treat missing, duplicate, malformed, or oversized payloads as `DBT_COMMAND_FAILED`.

Compare ordered identifiers and exact normalized data types in Python before data comparison.

### 16.7 Comparator macro

The comparator macro:

- obtains both relations via fixed `ref('baseline')`/`ref('candidate')` calls;
- retrieves and quotes columns through the adapter;
- verifies a generated internal column name does not collide;
- renders an adapter-selected exact comparison statement;
- executes it once through `run_query`;
- asserts exactly one result row;
- emits counts through the marked JSON channel.

For PostgreSQL `EXCEPT ALL`, use one statement conceptually equivalent to:

```sql
with
a as (
    select <explicit ordered columns> from <baseline_view>
),
b as (
    select <explicit ordered columns> from <candidate_view>
),
a_minus_b as (
    select * from a
    except all
    select * from b
),
b_minus_a as (
    select * from b
    except all
    select * from a
)
select
    (select count(*) from a) as baseline_rows,
    (select count(*) from b) as candidate_rows,
    (select count(*) from a_minus_b) as baseline_only_occurrences,
    (select count(*) from b_minus_a) as candidate_only_occurrences
```

This is one statement even though it contains scalar subqueries. Do not add `LIMIT` to the verdict query.

### 16.8 Diagnostic differences

If the exact verdict is different, an optional second query may retrieve a bounded sample. Because it may observe a later snapshot, label it:

```text
diagnostic_snapshot_different_from_verdict: true
```

Do not let diagnostic failure alter the already established verdict. Never persist full sensitive rows by default.

### 16.9 Cleanup macro

Normal cleanup drops `ref('baseline')` and `ref('candidate')` through adapter relation objects. Crash recovery loads the exact ledger, revalidates scratch database/schema and run ownership, resolves each exact relation, and drops only those objects.

Leave the scratch schema in place unless the current run created it, proved it empty, and the adapter implementation has an independently tested safe schema-drop path. Dropping two views is sufficient for v0.1.

Cleanup failure is reported prominently but does not rewrite an equality verdict as a data difference.

## 17. Comparator implementation

### 17.1 API

`verification/comparator.py`:

```python
class Comparator(Protocol):
    def normalize_schema(
        self,
        raw: RawSchemaPayload,
    ) -> RelationSchema: ...

    def validate_types(
        self,
        schema: RelationSchema,
        capabilities: VerificationCapabilities,
    ) -> tuple[ReasonCode, ...]: ...

    def generate_macro(
        self,
        schema: RelationSchema,
        strategy: BagStrategy,
        result_nonce: str,
    ) -> bytes: ...

    def parse_result(self, output: str, result_nonce: str) -> EqualityResult: ...
```

### 17.2 Exact result validation

Validate:

- counts are non-negative integers within the adapter’s supported range;
- baseline and candidate row counts agree with directional differences logically;
- status is equivalent only when schema matches and both directional counts are zero;
- no float/decimal coercion occurred in the result transport;
- no result field is silently missing or extra under the versioned output schema.

Do not trust a macro-emitted Boolean `verified`; derive it in Python.

### 17.3 Grouped-count fallback

Implement the signed-side grouped strategy before adding Snowflake or BigQuery. It must:

- use explicit ordered, adapter-quoted columns;
- append a collision-free side marker;
- `UNION ALL` baseline and candidate;
- group by every output column;
- compute per-value baseline and candidate multiplicity;
- sum positive directional deltas;
- return totals and directional differences in one statement.

Nulls form one SQL group and require no full-outer-join null predicate. Complex and floating types remain unsupported until adapter conformance tests pass.

### 17.4 Why existing macros are references only

The current `dbt_utils.equality` implementation uses distinct `EXCEPT`, which loses duplicate multiplicity. It cannot authorize `fix`. `dbt-audit-helper` can inform diagnostics, but the safety comparator remains small, internal, versioned, and tested. [dbt-utils equality source](https://github.com/dbt-labs/dbt-utils/blob/main/macros/generic_tests/equality.sql)

## 18. End-to-end orchestrator

### 18.1 Service shape

`orchestrator.py`:

```python
class RefmergeService:
    def scan(self, request: ScanRequest) -> ScanReport: ...
    def check(self, request: CheckRequest) -> CheckReport: ...
    def fix(self, request: FixRequest) -> FixReport: ...
    def cleanup(self, request: CleanupRequest) -> CleanupReport: ...
```

The CLI contains no business logic beyond option parsing, service invocation, rendering, and exit-policy evaluation.

### 18.2 `scan` flow

```python
def scan(request: ScanRequest) -> ScanReport:
    config = load_config(request)
    project = discover_project(config.project_dir)
    source_files = discover_model_files(project)
    parsed_manifest = load_or_create_parse_manifest(project, config)

    findings = []
    for path in sorted(source_files):
        source = read_source(path)
        source_model = parse_source_model(source)
        findings.extend(detect_source_duplicates(source_model, parsed_manifest))

    return ScanReport(..., findings=tuple(sorted(findings)))
```

No compile fallback occurs silently.

### 18.3 `check` flow

```python
def check(request: CheckRequest) -> CheckReport:
    config = load_and_validate_check_config(request)

    with RunWorkspace.create(config) as ws:
        snapshot = ws.snapshot_project()
        dbt = DbtCli.from_config(config)
        context = build_compilation_context(snapshot, dbt, config)

        baseline = compile_selected_models(ws.source_snapshot, context, request.selector)
        artifact_view = load_manifest(baseline.manifest_path)
        candidates = analyze_selected_models(snapshot, artifact_view, config)

        results = []
        for model in deterministic_model_order(candidates):
            plan = build_combined_plan(model)
            candidate_bytes = apply_and_validate_plan(plan)
            write_candidate_copy(ws.candidate_project, plan, candidate_bytes)

            candidate = compile_exact_model(ws.candidate_project, context, model)
            validate_compiled_delta(baseline.for_model(model), candidate, plan)
            validate_volatility(candidate.compiled_ast, config)

            receipt = verify_with_harness(
                workspace=ws,
                baseline=baseline.for_model(model),
                candidate=candidate,
                plan=plan,
                config=config,
            )
            results.append(receipt)

        return CheckReport(..., results=tuple(results))
```

For v0.1, process verification candidates sequentially and force harness dbt threads to one. Parallel warehouse execution is a later cost/concurrency feature.

### 18.4 `fix` flow

v0.1 requires one explicit source path:

```python
def fix(request: FixRequest) -> FixReport:
    check_report = check(request.as_check_request())
    receipt = check_report.single_model_receipt()

    if not is_fixable(receipt):
        return FixReport.not_applied(receipt)

    if request.dry_run:
        return FixReport.dry_run(receipt, check_report.diff)

    apply_verified_source(
        path=request.path,
        expected_original_sha256=receipt.original_source_sha256,
        candidate_bytes=check_report.candidate_bytes,
        expected_candidate_sha256=receipt.candidate_source_sha256,
    )
    return FixReport.applied(receipt)
```

Do not implement `fix --all` until multi-file failure/recovery semantics are explicitly designed.

### 18.5 Cleanup discipline

Use nested `try/finally` or context managers so that:

- warehouse cleanup is attempted after any post-DDL failure;
- file cleanup occurs after warehouse cleanup attempts;
- Ctrl-C still attempts bounded cleanup;
- cleanup exceptions are accumulated rather than hiding the primary exception;
- the final report lists every object as dropped, missing, retained, or cleanup-failed.

## 19. Reporting and JSON

### 19.1 Human output

For each model, show:

- source path and model unique id;
- upstream dbt resource unique id;
- duplicate CTE names;
- static checks and reason codes;
- proposed source diff;
- dbt/adapter/SQLGlot versions;
- scratch boundary and created objects;
- schema result;
- baseline and candidate row counts;
- directional occurrence differences;
- exact verification scope;
- cleanup result;
- whether the candidate is fixable.

Do not print raw differing rows by default.

### 19.2 Versioned JSON

Top-level shape:

```json
{
  "schema_version": "1",
  "command": "check",
  "run_id": "...",
  "project_dir": "...",
  "dbt": {
    "version": "...",
    "adapter_type": "postgres",
    "manifest_schema_version": "..."
  },
  "summary": {
    "models_scanned": 1,
    "findings": 1,
    "fixable": 1,
    "different": 0,
    "unverifiable": 0,
    "errors": 0
  },
  "models": [],
  "cleanup": {
    "complete": true,
    "objects": []
  }
}
```

Serialize paths consistently, sort arrays deterministically, use integers for counts, and never include credentials or complete environment mappings.

### 19.3 Diff rendering

Produce a unified diff from decoded source only after candidate byte validation. The diff is presentation; edits and hashes remain authoritative.

For JSON, include either:

- a UTF-8 unified diff when source is supported UTF-8; or
- structured edit spans and replacement text.

Do not add terminal color sequences to redirected output.

## 20. Safe source application

### 20.1 Preconditions

`apply_verified_source` must:

1. reject symlinks and non-regular files;
2. acquire a cross-platform advisory exclusive lock;
3. open without following a symlink where the OS supports it;
4. read current bytes and compare SHA-256 with the receipt;
5. verify candidate bytes against the candidate digest;
6. preserve file mode and relevant metadata;
7. write a temporary file in the same directory;
8. flush and `fsync` the temporary file;
9. re-stat the destination and detect replacement/change since step 4;
10. atomically replace with `os.replace`;
11. `fsync` the containing directory where supported;
12. release the lock.

POSIX filesystems do not provide a universal atomic “replace only if content hash still equals X” operation. The lock plus immediate hash/stat recheck is the supported best-effort precondition. Document that uncooperative concurrent writers are outside the lock protocol.

### 20.2 Failure behavior

On a changed file, return `SOURCE_CHANGED_BEFORE_APPLY` and leave both source and temporary candidate unchanged/cleaned as appropriate. Never rebase or merge automatically.

The tool does not require a clean Git tree. Git status and diff may be displayed as advisory context only.

## 21. Test plan

### 21.1 Unit-test matrix

`tests/unit/test_source_jinja.py`:

- every Jinja delimiter and whitespace-control form;
- quoted braces inside Jinja strings;
- raw blocks;
- unterminated expressions/statements/comments;
- literal and dynamic ref/source calls;
- byte offsets with multibyte Unicode before/inside/after a span.

`tests/unit/test_cte_splitter.py`:

- one/many CTEs;
- nested parentheses;
- strings/comments containing parentheses and commas;
- quoted CTE names;
- recursive CTE rejection;
- materialization hints and CTE column-list rejection;
- first/middle/last donor separator ownership.

`tests/unit/test_semantics.py`:

- every allowed import AST node;
- one fixture for every rejected clause/node;
- dialect identifier normalization;
- projection aliases/collisions;
- predicate fingerprints;
- nested CTE scope/shadowing;
- injected ephemeral CTEs;
- volatility functions and unknown UDFs.

`tests/unit/test_rewrite.py`:

- canonical selection;
- projection union ordering;
- donor removal;
- alias-preserving references;
- overlapping edit rejection;
- comment relocation rejection;
- no byte changes outside spans;
- reparse after patch.

`tests/unit/test_artifacts.py`:

- one fixture per supported manifest schema;
- unknown future version;
- missing/extra/null fields;
- duplicate/path-traversal cases;
- ambiguous ref resolution.

`tests/unit/test_harness.py`:

- generated project structure;
- raw-block terminator rejection;
- exact node allowlist;
- scratch database/schema escape;
- manifest preflight;
- result-marker parsing;
- cleanup ledger validation.

`tests/unit/test_comparator.py`:

- exact schema order/name/type;
- unsupported types;
- zero/equal/different counts;
- derivation of fixability;
- malformed or adversarial macro output.

### 21.2 Golden fixtures

Each case contains:

```text
case_name/
├── input.sql
├── expected.sql
├── expected_edits.json
└── expected_result.json
```

Required cases:

- two identical projections;
- disjoint projections;
- overlapping projections;
- direct aliases;
- projection collision;
- equal filters with formatting differences;
- reordered predicates rejected;
- different predicates;
- downstream reference with alias;
- downstream reference without alias;
- donor referenced multiple times;
- nested same-name CTE;
- canonical/donor star observation;
- comments on projection;
- comment that would be deleted;
- CRLF, BOM, Unicode, and no final newline;
- multiple groups combined in one candidate;
- Jinja control flow and macro-generated SQL rejection.

### 21.3 Property tests

Use Hypothesis to vary:

- whitespace/newline/comment placement;
- safe identifier spelling and quoting;
- projection ordering and overlap;
- CTE counts and donor positions;
- Unicode prefix lengths.

Properties:

- applying the same plan twice is impossible or a no-op with no remaining finding;
- every unchanged byte range is identical;
- edit spans never overlap;
- candidate source hash is deterministic;
- semantic fingerprint is stable for permitted formatting variation;
- grouping/order output is stable across randomized input enumeration.

### 21.4 dbt project integration fixtures

Build tiny projects for:

```text
identical_imports
different_projections
equivalent_filters
different_filters
aliases
affected_star
nested_ctes
macro_generated
compile_introspection
ephemeral_dependency
incremental_target
volatile_model
multiple_groups
local_package
```

For every project:

- run real `dbt parse`;
- run real baseline and candidate compilation;
- assert artifact loader compatibility;
- assert compiled-delta outcome;
- prove the user fixture tree hash is unchanged after `scan` and `check`.

### 21.5 PostgreSQL warehouse tests

Run a containerized PostgreSQL service and a dedicated dbt profile with only scratch/test permissions.

Comparator datasets:

```text
empty == empty
A == A
A != B
A,A,B == A,A,B
A,A,B != A,B,B
NULL,NULL == NULL,NULL
NULL != non-NULL
same values, different column order
same values, different exact type
large duplicate multiplicities
timestamps and exact decimals
every certified type
every explicitly unsupported type
```

Concurrency test:

1. create an upstream table;
2. create baseline/candidate views;
3. start the comparison statement and hold it long enough to overlap a writer;
4. commit an upstream change from another connection;
5. prove both sides of the statement used one snapshot;
6. run a second statement and prove it can see the later state.

### 21.6 Failure-injection tests

Inject failure after:

- workspace creation;
- source snapshot;
- baseline compile;
- candidate patch;
- candidate compile;
- first view creation;
- second view creation;
- schema probe;
- comparison;
- source temp-file write;
- source pre-replace validation.

Assert source integrity, exact ledger state, bounded cleanup, and correct primary/cleanup error reporting.

## 22. CI and release engineering

### 22.1 Pull-request CI

Required jobs:

```text
format/lint
type-check
unit + property tests on Python 3.11
unit + golden tests on newest supported Python
artifact fixture compatibility
dbt-postgres integration
PostgreSQL comparator conformance
package build/install/smoke test
```

Run supported OS jobs for Linux and at least one of macOS/Windows before claiming cross-platform source application.

### 22.2 Dependency updates

Automated dependency PRs must run the complete suite. Changes to these packages require explicit maintainer review of golden/semantic snapshots:

- SQLGlot;
- Jinja2;
- Typer/Click;
- Pydantic;
- dbt-postgres in integration tests.

SQLGlot upgrades require rebuilding and reviewing the AST fingerprint fixture corpus.

### 22.3 Security checks

Add:

- dependency vulnerability scanning;
- secret scanning;
- package provenance/signing appropriate to the release platform;
- tests proving JSON/log redaction;
- fuzz/property limits for source and artifact parsers.

### 22.4 Release artifacts

Before publishing:

- build wheel and source distribution;
- install the wheel in a clean environment without dbt Python packages;
- point `--dbt-command` at a separate test environment;
- run `scan`, `check`, `fix --dry-run`, and one actual fixture `fix`;
- verify help text and exit policies;
- generate a machine-readable support matrix;
- publish known limitations and verification semantics prominently.

## 23. Milestone plan and acceptance criteria

### Milestone 0 — skeleton and contracts

Deliver:

- package/CLI skeleton;
- config, reason codes, domain objects;
- dbt process wrapper and version discovery;
- CI, lint, typing, test framework.

Exit criteria:

- wheel installs and `dbt-refmerge --help` runs;
- no runtime dbt Python dependency;
- subprocess timeout/cancellation/redaction tests pass;
- JSON schema version is defined.

### Milestone 1 — source-only scanner

Deliver:

- stable workspace snapshot;
- Jinja masking and literal calls;
- CTE splitter/import parser;
- parse-manifest loading and dependency resolution;
- `scan` human/JSON output.

Exit criteria:

- `scan` makes no warehouse connection in integration tests;
- all source offsets pass Unicode/CRLF property tests;
- every unsupported shape has a stable reason;
- fixture working-tree hashes remain unchanged.

### Milestone 2 — compiled semantic qualification

Deliver:

- isolated dbt compilation;
- manifest compatibility adapters;
- PostgreSQL SQLGlot analyzer;
- positive AST allowlist;
- predicate fingerprints;
- scope-aware donor reference binding;
- whole-model volatility gate.

Exit criteria:

- every negative safety fixture is rejected;
- source/compiled mapping is one-to-one or fails closed;
- unknown SQLGlot nodes never pass;
- compilation requiring introspection is explicit.

### Milestone 3 — rewrite and compile-delta gate

Deliver:

- pure combined planner;
- minimal source patcher;
- alias-preserving reference edits;
- golden/property tests;
- expected compiled-AST transform and full fingerprint comparison.

Exit criteria:

- no unchanged byte differs in golden/property tests;
- comments are preserved or candidate is rejected;
- multiple groups are verified as one candidate;
- every deliberate unexpected compile mutation yields `COMPILE_DRIFT`.

### Milestone 4 — PostgreSQL exact verification

Deliver:

- minimal harness generator;
- scratch manifest preflight;
- view creation and schema probe;
- `EXCEPT ALL` and grouped-count comparators;
- result-marker parser;
- ledger and exact cleanup;
- `check` end to end.

Exit criteria:

- duplicate multiplicity adversarial cases pass;
- schema mismatch cannot be hidden by coercion;
- concurrency test proves one-statement snapshot behavior;
- unsupported types cannot produce success;
- crash injection never touches source and leaves an actionable ledger;
- scratch escape tests fail before DDL.

### Milestone 5 — safe `fix`

Deliver:

- one-path `fix` and `--dry-run`;
- receipt binding;
- advisory lock/hash/stat precondition;
- atomic same-directory replacement;
- final source reparse and report.

Exit criteria:

- changing one byte between verification and apply prevents the write;
- dirty/non-Git projects work;
- verified candidate bytes and written bytes have the same digest;
- interrupted writes never leave a partial target file.

### Milestone 6 — v0.1 release hardening

Deliver:

- docs and examples;
- stable JSON/exit contract;
- performance profiling of the tool itself;
- install smoke tests;
- security and support statements;
- cleanup recovery command.

Exit criteria:

- all release gates in Section 24 pass;
- at least two real-world PostgreSQL dbt projects complete read-only `scan`/`check` trials;
- every discovered false positive becomes a blocking regression fixture;
- maintainers sign off on the threat model and guarantee wording.

### Milestone 7 — Snowflake

After v0.1:

- implement Snowflake identifier/type normalization;
- use grouped counts unless a tested exact bag primitive exists for the deployed Snowflake version;
- add transient scratch object policy, query tags, statement timeout, and cost reporting;
- certify one-statement view comparison;
- test variants/floats only after exact semantics are explicitly defined;
- add real Snowflake CI with tightly scoped credentials;
- investigate time-travel/clone support for future incremental verification.

If first design partners require Snowflake, Milestones 4 and 7 may be reversed; do not skip the reproducible PostgreSQL core test lane.

## 24. v0.1 release gates

All must be true:

### Correctness

- Every automatic candidate belongs to the documented import grammar.
- Source/compiled mapping is unique.
- Expected and actual candidate compiled AST fingerprints match.
- Whole-model volatility gate passes.
- Ordered schemas match exactly.
- Both directional bag differences are zero.
- Candidate receipt and source bytes are digest-bound.

### Safety

- `scan` is warehouse-free by default.
- `check` never writes source.
- Scratch relation preflight occurs before DDL.
- No test can escape the scratch schema.
- Cleanup targets exact ledger objects.
- Source races prevent application.
- Secrets are absent from logs and JSON fixtures.

### Compatibility

- Each advertised Python/dbt artifact/PostgreSQL version has a CI lane or documented test evidence.
- Unknown manifest versions fail with a clear message.
- The wheel has no dbt runtime dependency.
- User dbt executable selection is recorded and reproducible.

### Operability

- Timeouts and Ctrl-C work.
- Cleanup failures are actionable.
- JSON output is versioned and deterministic.
- Exit policy is documented.
- Warehouse queries carry a run identity where supported.
- Known cost behavior and limitations are documented.

### Product honesty

- Output says snapshot-equivalent, not universally proven.
- No performance improvement is implied.
- Incremental/ephemeral limitations are visible.
- Exact supported output types are published.
- Diagnostic samples are distinguished from the verdict snapshot.

## 25. Example end-to-end implementation trace

Given:

```sql
with orders as (

    select
        order_id,
        customer_id

    from {{ ref('stg_orders') }}

),

order_financials as (

    select
        order_id,
        amount,
        tax

    from {{ ref('stg_orders') }}

),

final as (

    select
        orders.order_id,
        orders.customer_id,
        order_financials.amount,
        order_financials.tax

    from orders
    left join order_financials
        on orders.order_id = order_financials.order_id

)

select * from final
```

The implementation performs:

1. Source scanner finds `orders` and `order_financials` with literal identical refs.
2. Manifest resolver maps both to the same `model.<project>.stg_orders` unique id.
3. Baseline compile produces one PostgreSQL query.
4. SQLGlot confirms both are direct imports with absent predicates.
5. Projection maps are:

   ```text
   orders: order_id->order_id, customer_id->customer_id
   order_financials: order_id->order_id, amount->amount, tax->tax
   ```

6. Planner chooses `orders`, appends `amount` and `tax`, and deletes the donor CTE.
7. The unaliased downstream donor reference becomes:

   ```sql
   left join orders as order_financials
   ```

8. The final CTE’s existing `order_financials.amount` qualifiers remain correct.
9. Candidate source is compiled.
10. Expected compiled AST transform exactly matches actual candidate AST.
11. Harness creates `baseline_<run>` and `candidate_<run>` scratch views.
12. Schema probe confirms identical ordered output columns/types.
13. One `EXCEPT ALL` comparison statement reports:

   ```text
   baseline_rows = 4,198,221
   candidate_rows = 4,198,221
   baseline_only_occurrences = 0
   candidate_only_occurrences = 0
   ```

14. Receipt status is `SNAPSHOT_EQUIVALENT`.
15. `check` drops both views and reports the diff.
16. A later explicit `fix <path>` repeats the pipeline.
17. The source lock/hash precondition succeeds.
18. The exact candidate bytes are atomically installed.

If the final `select * from final` were instead `select orders.* ...`, eligibility would fail because adding columns to `orders` would alter the star expansion.

## 26. Common implementation mistakes to avoid

- Running `dbt compile` inside default `scan` and still calling it warehouse-free.
- Treating a manifest as fresh merely because it exists in `target/`.
- Matching source and compiled CTEs only by lowercased name.
- Using SQLGlot-generated SQL as the source rewrite.
- Checking for forbidden syntax instead of validating an allowed AST grammar.
- Comparing only columns used downstream.
- Globally replacing donor names or qualifiers.
- Losing comments inside a deleted CTE.
- Compiling shadow models under different dbt node names and assuming `this`/`model` are unchanged.
- Running baseline and candidate tables sequentially and calling counts a snapshot guarantee.
- Assuming BigQuery or Snowflake supports `EXCEPT ALL` because another engine does.
- Comparing schema only after a set operation has coerced types.
- Supporting floats before defining `NaN` and signed-zero semantics.
- Adding `LIMIT` to the verdict query.
- Letting a diagnostic sample determine equality.
- Trusting a target named `dev` or blocking one merely because it is named `prod`.
- Dropping every object matching `dbt_refmerge_tmp_*`.
- Requiring a clean Git tree instead of checking exact source bytes.
- Verifying duplicate groups separately but writing their unverified combination.
- Swallowing cleanup errors or allowing them to hide the primary failure.
- Adding five adapter subclasses before one adapter passes conformance tests.

## 27. Maintainer decision log

Record architecture-affecting decisions in short ADR files once implementation begins. Initial ADRs should cover:

```text
ADR-001 subprocess/artifact dbt boundary
ADR-002 source byte spans and no AST source serialization
ADR-003 PostgreSQL as v0.1 reference adapter
ADR-004 one-statement scratch-view comparison
ADR-005 exact bag equality and no probabilistic fix gate
ADR-006 incremental/ephemeral v0.1 exclusion
ADR-007 source application lock/hash/atomic-replace protocol
ADR-008 stable reason-code and JSON versioning policy
```

An ADR change that weakens a safety invariant requires new negative tests and an explicit release note.

## 28. Primary implementation references

- [Final architecture](./dbt-refmerge_finalized_codex.md)
- [dbt compile behavior and introspection](https://docs.getdbt.com/reference/commands/compile)
- [dbt parse behavior](https://docs.getdbt.com/reference/commands/parse)
- [dbt programmatic invocation and process-safety guidance](https://docs.getdbt.com/reference/programmatic-invocations)
- [dbt manifest JSON](https://docs.getdbt.com/reference/artifacts/manifest-json)
- [dbt JSON schema catalog](https://schemas.getdbt.com/)
- [SQLGlot](https://github.com/tobymao/sqlglot)
- [SQLGlot AST/scope primer](https://github.com/tobymao/sqlglot/blob/main/posts/ast_primer.md)
- [SQLFluff dbt templater](https://docs.sqlfluff.com/en/stable/configuration/templating/dbt.html)
- [dbt-utils equality implementation](https://github.com/dbt-labs/dbt-utils/blob/main/macros/generic_tests/equality.sql)
- [PostgreSQL transaction isolation](https://www.postgresql.org/docs/current/transaction-iso.html)
- [Snowflake set operators](https://docs.snowflake.com/en/sql-reference/operators-query)
- [Snowflake cloning](https://docs.snowflake.com/en/sql-reference/sql/create-clone)
