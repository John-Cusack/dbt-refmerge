"""Warehouse verification (PostgreSQL) for a batch of models, and cleanup of a run's scratch relations.

One pass for the whole batch, so the number of dbt invocations does not grow with the number of models:

1. Validate the scratch schema and write a throwaway dbt project (``harness``) holding, for every model,
   its baseline and candidate compiled SQL as two views aliased with a per-run, per-model token.
2. ``dbt parse`` and preflight the manifest: exactly those views, no hooks, inside the scratch schema.
   Only then record the relations in the ledger and ``dbt run`` them.
3. Read every view's column names and exact types from ``pg_catalog`` in one query. Unsupported types
   refuse; a schema difference is DIFFERENT.
4. Compare each model's two views as bags, each in ONE statement (one snapshot), all from one dbt call.
5. Drop the views (views only, by exact name) and confirm they are gone, whatever happened above; both in
   one dbt invocation.

Failures stay per model: a view dbt could not build fails only its model (``run_results.json``), and if
the batched comparison fails, each comparison is retried alone so one bad query cannot hide the rest.

Every query runs through the harness ``dbt_refmerge_query`` macro, so dbt-refmerge needs no database
driver: dbt and the user's profile provide the connection.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from importlib.metadata import version as _pkg_version
from pathlib import Path
from typing import Any

from dbt_refmerge.adapters import AdapterSpec, read_project_profile
from dbt_refmerge.artifacts import ManifestNodeModel, ManifestView
from dbt_refmerge.config import AppConfig, CompilationContext
from dbt_refmerge.dbt_cli import DbtCli, DbtInvocation
from dbt_refmerge.domain import (
    EqualityResult,
    IdentifierIdentity,
    ReasonCode,
    RelationIdentity,
    RewritePlan,
    VerificationReceipt,
    VerificationStatus,
)
from dbt_refmerge.errors import DbtError, RefmergeError, ScratchBoundaryError, VerificationError
from dbt_refmerge.verification.capabilities import COMPARATOR_VERSION, POSTGRES_EXACT_TYPES
from dbt_refmerge.verification.comparator import (
    RawSchemaPayload,
    derive_status,
    normalize_schema,
    parse_equality_result,
    parse_marked_json,
    quote_ident,
    schemas_equal,
    validate_types,
)
from dbt_refmerge.verification.harness import (
    BASELINE_ALIAS_PREFIX,
    CANDIDATE_ALIAS_PREFIX,
    DBT_INTERMEDIATE_SUFFIXES,
    SCRATCH_SCHEMA_VAR,
    _sanitize_run_token,
    build_verdict_sql,
    harness_node_id,
    preflight_harness_manifest,
    relation_token,
    scratch_identifiers,
    validate_scratch_schema,
    write_harness_macros,
    write_harness_pair,
)
from dbt_refmerge.workspace import RunWorkspace, ScratchObject

RUN_ID_PATTERN = re.compile(r"\d{8}T\d{6}_[0-9a-f]{12}")
_NO_EQUALITY = EqualityResult(False, 0, 0, 0, 0)


@dataclass(frozen=True)
class VerificationRequest:
    config: AppConfig
    workspace: RunWorkspace
    dbt: DbtCli
    context: CompilationContext
    view: ManifestView
    baseline: ManifestNodeModel
    candidate: ManifestNodeModel
    plan: RewritePlan
    spec: AdapterSpec


# Verifies a batch sharing one config, workspace, dbt, context, manifest and adapter; one receipt per request.
VerifyRunner = Callable[[Sequence[VerificationRequest]], list[VerificationReceipt]]
Outcome = tuple[VerificationStatus, tuple[ReasonCode, ...], EqualityResult]


@dataclass(frozen=True)
class QueryResult:
    columns: tuple[str, ...]
    rows: tuple[tuple[str | None, ...], ...]


def sql_literal(value: str) -> str:
    """A standard SQL string literal; refuses characters whose meaning depends on server settings."""
    if "\\" in value or "\x00" in value:
        raise VerificationError(ReasonCode.HARNESS_EMBEDDING_UNSAFE, f"unsupported character in {value!r}")
    return "'" + value.replace("'", "''") + "'"


def catalog_sql(schema: str, identifiers: Sequence[str]) -> str:
    """Kind and columns (exact types, in ordinal order) of the named relations in ``schema``."""
    names = ", ".join(sql_literal(name) for name in identifiers)
    return (
        "select c.relname, c.relkind::text, a.attnum, a.attname, format_type(a.atttypid, a.atttypmod)\n"
        "from pg_catalog.pg_class c\n"
        "join pg_catalog.pg_namespace n on n.oid = c.relnamespace\n"
        "left join pg_catalog.pg_attribute a on a.attrelid = c.oid and a.attnum > 0 and not a.attisdropped\n"
        f"where n.nspname = {sql_literal(schema)} and c.relname in ({names})\n"
        "order by c.relname, a.attnum"
    )


def existing_relations_sql(schema: str, identifiers: Sequence[str]) -> str:
    """Which of the named relations (of any kind) exist in ``schema``."""
    names = ", ".join(sql_literal(name) for name in identifiers)
    return (
        "select c.relname\n"
        "from pg_catalog.pg_class c\n"
        "join pg_catalog.pg_namespace n on n.oid = c.relnamespace\n"
        f"where n.nspname = {sql_literal(schema)} and c.relname in ({names})\n"
        "order by c.relname"
    )


def run_relations_pattern(run_token: str) -> str:
    suffixes = "|".join(DBT_INTERMEDIATE_SUFFIXES)
    return f"^({BASELINE_ALIAS_PREFIX}|{CANDIDATE_ALIAS_PREFIX}){re.escape(run_token)}_[0-9a-f]{{8}}({suffixes})?$"


def run_views_sql(schema: str, run_token: str) -> str:
    return (
        "select c.relname\n"
        "from pg_catalog.pg_class c\n"
        "join pg_catalog.pg_namespace n on n.oid = c.relnamespace\n"
        f"where n.nspname = {sql_literal(schema)} and c.relkind = 'v'\n"
        f"  and c.relname ~ {sql_literal(run_relations_pattern(run_token))}\n"
        "order by c.relname"
    )


class HarnessSession:
    """dbt invocations against one harness project."""

    def __init__(self, dbt: DbtCli, root: Path, config: AppConfig, scratch_schema: str, target_path: Path) -> None:
        self.dbt = dbt
        self.root = root
        self.config = config
        self.target_path = target_path
        self.invocation = DbtInvocation(
            project_dir=root,
            profiles_dir=config.profiles_dir or _project_profiles_dir(config.project_dir),
            profile=config.profile,
            target=config.target,
            target_path=target_path,
            threads=1,
            vars_json=json.dumps({SCRATCH_SCHEMA_VAR: scratch_schema}),
            timeout_seconds=config.subprocess_timeout_seconds,
        )

    def parse(self) -> Path:
        res = self.dbt.parse(self.invocation)
        if res.returncode != 0:
            raise DbtError(f"dbt parse failed for the verification harness: {_tail(res)}", argv=res.argv_redacted)
        return self.target_path / "manifest.json"

    def run(self) -> set[str]:
        """Build every harness view; returns the unique_ids dbt could not build (from run_results.json)."""
        run_results = self.target_path / "run_results.json"
        run_results.unlink(missing_ok=True)
        res = self.dbt.run(self.invocation, "fqn:*")
        if res.returncode == 0:
            return set()
        failed = _failed_nodes(run_results)
        if not failed:
            raise DbtError(f"dbt run failed for the verification harness: {_tail(res)}", argv=res.argv_redacted)
        return failed

    def queries(self, label: str, statements: Mapping[str, str]) -> dict[str, QueryResult | RefmergeError]:
        """Run several statements from one dbt invocation; if that fails, retry each alone to attribute it."""
        nonce = secrets.token_hex(8)
        args = {
            "nonce": nonce,
            "queries": [{"key": key, "sql": sql} for key, sql in statements.items()],
            "statement_timeout_ms": self.config.warehouse_statement_timeout_ms,
            "label": label,
        }
        res = self.dbt.run_operation(self.invocation, "dbt_refmerge_queries", args)
        results: dict[str, QueryResult | RefmergeError] = {}
        for key, sql in statements.items():
            try:
                if res.returncode != 0:
                    results[key] = self.query(label, sql)
                else:
                    results[key] = _query_result(parse_marked_json(res.stdout, f"{nonce}_{key}"))
            except RefmergeError as exc:
                results[key] = exc
        return results

    def query(self, label: str, sql: str) -> QueryResult:
        nonce = secrets.token_hex(8)
        args = {
            "nonce": nonce,
            "sql": sql,
            "statement_timeout_ms": self.config.warehouse_statement_timeout_ms,
            "label": label,
        }
        res = self.dbt.run_operation(self.invocation, "dbt_refmerge_query", args)
        if res.returncode != 0:
            raise DbtError(f"warehouse query {label!r} failed: {_tail(res)}", argv=res.argv_redacted)
        return _query_result(parse_marked_json(res.stdout, nonce))

    def drop_views(self, schema: str, identifiers: Sequence[str]) -> set[str]:
        """Drop the named views and return the names that still exist afterwards (one dbt invocation)."""
        nonce = secrets.token_hex(8)
        args = {
            "nonce": nonce,
            "schema": schema,
            "identifiers": list(identifiers),
            "sql": existing_relations_sql(schema, identifiers),
            "statement_timeout_ms": self.config.warehouse_statement_timeout_ms,
        }
        res = self.dbt.run_operation(self.invocation, "dbt_refmerge_drop_views", args)
        if res.returncode != 0:
            raise DbtError(f"dropping scratch views failed: {_tail(res)}", argv=res.argv_redacted)
        return {row[0] for row in _query_result(parse_marked_json(res.stdout, nonce)).rows if row[0] is not None}


@dataclass(frozen=True)
class _Pair:
    """One request whose harness views were written."""

    index: int
    request: VerificationRequest
    token: str
    relations: tuple[RelationIdentity, RelationIdentity]

    @property
    def names(self) -> tuple[str, str]:
        return (self.relations[0].identifier.value, self.relations[1].identifier.value)


def verify_postgres(request: VerificationRequest) -> VerificationReceipt:
    """Verify a single model (a batch of one)."""
    return verify_postgres_batch([request])[0]


def verify_postgres_batch(requests: Sequence[VerificationRequest]) -> list[VerificationReceipt]:
    """One receipt per request, in order: one harness batch per model database.

    Every harness view lives in the profile's target database, and the preflight holds a batch to a single
    database. A batch per database keeps a model from another database from refusing the rest.
    """
    by_database: dict[str | None, list[int]] = {}
    for index, request in enumerate(requests):
        by_database.setdefault(request.baseline.database, []).append(index)
    receipts: dict[int, VerificationReceipt] = {}
    for indexes in by_database.values():
        receipts.update(zip(indexes, _verify_one_database([requests[i] for i in indexes]), strict=True))
    return [receipts[index] for index in range(len(requests))]


def _verify_one_database(requests: Sequence[VerificationRequest]) -> list[VerificationReceipt]:
    shared = requests[0]
    ws, config = shared.workspace, shared.config
    outcomes: dict[int, Outcome] = {}
    pairs: list[_Pair] = []
    created: list[_Pair] = []
    session: HarnessSession | None = None
    scratch_schema = ""
    cleaned: dict[int, bool] = {}
    try:
        scratch_schema = validate_scratch_schema(config.scratch_schema or "").value
        if shared.spec.capabilities is None:
            raise VerificationError(ReasonCode.UNSUPPORTED_ADAPTER, f"adapter {shared.spec.name!r} cannot verify")
        strategy = shared.spec.capabilities.bag_strategy
        write_harness_macros(ws.harness_project, _profile_name(config, shared.context.project_dir))
        for index, request in enumerate(requests):
            try:
                pairs.append(_write_pair(index, request, scratch_schema, {pair.token for pair in pairs}))
            except RefmergeError as exc:
                outcomes[index] = _failure(exc)
        if pairs:
            session = HarnessSession(shared.dbt, ws.harness_project, config, scratch_schema, _harness_target(ws))
            expected = {
                harness_node_id(role, pair.token): relation.identifier.value
                for pair in pairs
                for role, relation in zip(("baseline", "candidate"), pair.relations, strict=True)
            }
            preflight_harness_manifest(
                session.parse(), database=pairs[0].relations[0].database.value, schema=scratch_schema, expected=expected
            )
            for pair in pairs:
                for relation in pair.relations:
                    ws.record_object(ScratchObject(relation=relation, kind="view"))
            created = list(pairs)
            failed_nodes = session.run()
            built = []
            for pair in pairs:
                if {harness_node_id("baseline", pair.token), harness_node_id("candidate", pair.token)} & failed_nodes:
                    outcomes[pair.index] = _failure(DbtError("dbt could not build this model's harness views"))
                else:
                    built.append(pair)
            outcomes.update(_compare(session, built, scratch_schema, strategy))
    except RefmergeError as exc:
        for index in range(len(requests)):
            outcomes.setdefault(index, _failure(exc))
    finally:
        if created and session is not None:
            cleaned = _drop_run_relations(session, ws, scratch_schema, created)
    relations = {pair.index: pair.relations for pair in created}
    return [
        _receipt(request, *outcomes[index], relations.get(index, ()), cleaned.get(index, index not in relations))
        for index, request in enumerate(requests)
    ]


def _write_pair(index: int, request: VerificationRequest, scratch_schema: str, taken: set[str]) -> _Pair:
    validate_scratch_schema(request.config.scratch_schema or "", model_schema=request.baseline.schema_)
    database = request.baseline.database
    if not database:
        raise ScratchBoundaryError(f"model {request.baseline.unique_id} has no database in the manifest")
    token = relation_token(request.workspace.run_id, request.baseline.unique_id)
    if token in taken:
        # Two models whose ids share a hash prefix would share views, and one would be judged on the other's SQL.
        raise ScratchBoundaryError(
            f"scratch relation names for {request.baseline.unique_id} collide with another model"
        )
    harness = write_harness_pair(
        request.workspace.harness_project,
        token,
        request.baseline.compiled_code or "",
        request.candidate.compiled_code or "",
    )
    baseline, candidate = (
        RelationIdentity(IdentifierIdentity(database), IdentifierIdentity(scratch_schema), IdentifierIdentity(alias))
        for alias in (harness.baseline_alias, harness.candidate_alias)
    )
    return _Pair(index=index, request=request, token=token, relations=(baseline, candidate))


def _failure(exc: RefmergeError) -> Outcome:
    status = VerificationStatus.ERROR if isinstance(exc, DbtError) else VerificationStatus.UNVERIFIABLE
    return status, (exc.reason_code,), _NO_EQUALITY


def cleanup_run(config: AppConfig, dbt: DbtCli, run_id: str, ws: RunWorkspace) -> dict[str, Any]:
    """Drop every scratch view a ``check`` run with ``run_id`` may have left behind."""
    if not RUN_ID_PATTERN.fullmatch(run_id):
        raise RefmergeError(ReasonCode.CLEANUP_FAILED, f"invalid run id {run_id!r}")
    scratch_schema = validate_scratch_schema(config.scratch_schema or "").value
    write_harness_macros(ws.harness_project, _profile_name(config, config.project_dir))
    session = HarnessSession(dbt, ws.harness_project, config, scratch_schema, ws.artifacts_root / "cleanup-target")
    pattern = re.compile(run_relations_pattern(_sanitize_run_token(run_id)))
    found = session.query("run-views", run_views_sql(scratch_schema, _sanitize_run_token(run_id)))
    names = sorted({row[0] for row in found.rows if row[0] is not None and pattern.fullmatch(row[0])})
    remaining = sorted(session.drop_views(scratch_schema, names)) if names else []
    return {
        "run_id": run_id,
        "schema": scratch_schema,
        "dropped": [name for name in names if name not in remaining],
        "remaining": remaining,
        "complete": not remaining,
    }


def _compare(session: HarnessSession, pairs: list[_Pair], schema: str, strategy: str) -> dict[int, Outcome]:
    if not pairs:
        return {}
    catalog = session.query("schema", catalog_sql(schema, [name for pair in pairs for name in pair.names]))
    outcomes: dict[int, Outcome] = {}
    verdicts: dict[str, str] = {}
    for pair in pairs:
        try:
            baseline_name, candidate_name = pair.names
            rows = tuple(row for row in catalog.rows if row[0] in pair.names)
            payload = _schema_payload(QueryResult(catalog.columns, rows), baseline_name, candidate_name)
            baseline_schema = normalize_schema(payload)
            candidate_schema = normalize_schema(RawSchemaPayload(baseline=payload.candidate, candidate=[]))
            if any(
                validate_types(relation_schema, POSTGRES_EXACT_TYPES) != (ReasonCode.OK,)
                for relation_schema in (baseline_schema, candidate_schema)
            ):
                outcomes[pair.index] = (
                    VerificationStatus.UNVERIFIABLE,
                    (ReasonCode.UNSUPPORTED_COMPARISON_TYPE,),
                    _NO_EQUALITY,
                )
            elif not schemas_equal(baseline_schema, payload):
                outcomes[pair.index] = (VerificationStatus.DIFFERENT, (ReasonCode.SCHEMA_MISMATCH,), _NO_EQUALITY)
            else:
                columns = [column.name for column in baseline_schema.columns]
                baseline, candidate = pair.relations
                verdicts[pair.token] = build_verdict_sql(_quoted(baseline), _quoted(candidate), columns, strategy)
        except RefmergeError as exc:
            outcomes[pair.index] = _failure(exc)
    results = session.queries("verdict", verdicts) if verdicts else {}
    for pair in pairs:
        result = results.get(pair.token)
        if result is None:
            continue
        try:
            if isinstance(result, RefmergeError):
                raise result
            equality = parse_equality_result({"schema_equal": True, **_counts(result)})
        except RefmergeError as exc:
            outcomes[pair.index] = _failure(exc)
            continue
        status = VerificationStatus(derive_status(equality))
        codes = (ReasonCode.OK,) if status is VerificationStatus.SNAPSHOT_EQUIVALENT else (ReasonCode.BAG_DIFFERENCE,)
        outcomes[pair.index] = (status, codes, equality)
    return outcomes


def _drop_run_relations(session: HarnessSession, ws: RunWorkspace, schema: str, pairs: list[_Pair]) -> dict[int, bool]:
    """Drop every view the batch may have created; per request, whether all of its names are gone."""
    names = [name for pair in pairs for name in scratch_identifiers(pair.token)]
    try:
        remaining = session.drop_views(schema, names)
    except RefmergeError:
        remaining = set(names)
    cleaned: dict[int, bool] = {}
    for pair in pairs:
        cleaned[pair.index] = not remaining.intersection(scratch_identifiers(pair.token))
        for relation in pair.relations:
            state = "cleanup_failed" if relation.identifier.value in remaining else "dropped"
            ws.record_object(ScratchObject(relation=relation, kind="view", state=state))
    return cleaned


def _failed_nodes(run_results: Path) -> set[str]:
    """unique_ids whose status in dbt's run_results.json is not success; empty when unreadable."""
    try:
        results = json.loads(run_results.read_text(encoding="utf-8"))["results"]
        return {
            str(result["unique_id"])
            for result in results
            if isinstance(result, dict) and result.get("status") != "success"
        }
    except (OSError, ValueError, KeyError, TypeError):
        return set()


def _schema_payload(result: QueryResult, baseline: str, candidate: str) -> RawSchemaPayload:
    columns: dict[str, list[dict[str, Any]]] = {baseline: [], candidate: []}
    kinds: dict[str, str | None] = {}
    for relname, relkind, attnum, attname, data_type in result.rows:
        if relname not in columns:
            raise VerificationError(ReasonCode.DBT_COMMAND_FAILED, f"unexpected relation in catalog result: {relname}")
        kinds[relname] = relkind
        if attnum is None:
            continue
        if attname is None or data_type is None or not attnum.isdigit():
            raise VerificationError(ReasonCode.DBT_COMMAND_FAILED, f"malformed catalog row for {relname}")
        columns[relname].append({"ordinal": int(attnum), "name": attname, "data_type": data_type})
    for relname in (baseline, candidate):
        if kinds.get(relname) != "v" or not columns[relname]:
            raise ScratchBoundaryError(f"harness relation {relname} is not a view with columns")
    return RawSchemaPayload(baseline=columns[baseline], candidate=columns[candidate])


def _counts(result: QueryResult) -> dict[str, int]:
    expected = ("baseline_rows", "candidate_rows", "baseline_only_occurrences", "candidate_only_occurrences")
    if result.columns != expected or len(result.rows) != 1:
        raise VerificationError(ReasonCode.DBT_COMMAND_FAILED, "verdict query returned an unexpected shape")
    counts: dict[str, int] = {}
    for name, value in zip(expected, result.rows[0], strict=True):
        if value is None or not re.fullmatch(r"\d{1,19}", value):
            raise VerificationError(ReasonCode.DBT_COMMAND_FAILED, f"invalid count {name}: {value!r}")
        counts[name] = int(value)
    return counts


def _query_result(payload: dict[str, Any]) -> QueryResult:
    columns = payload.get("columns")
    rows = payload.get("rows")
    if set(payload) != {"columns", "rows"} or not isinstance(columns, list) or not isinstance(rows, list):
        raise VerificationError(ReasonCode.DBT_COMMAND_FAILED, "query result must have columns and rows")
    if not all(isinstance(c, str) for c in columns):
        raise VerificationError(ReasonCode.DBT_COMMAND_FAILED, "query result column names must be strings")
    parsed_rows: list[tuple[str | None, ...]] = []
    for row in rows:
        if not isinstance(row, list) or len(row) != len(columns):
            raise VerificationError(ReasonCode.DBT_COMMAND_FAILED, "query result row has the wrong width")
        if not all(value is None or isinstance(value, str) for value in row):
            raise VerificationError(ReasonCode.DBT_COMMAND_FAILED, "query result values must be strings or null")
        parsed_rows.append(tuple(row))
    return QueryResult(columns=tuple(columns), rows=tuple(parsed_rows))


def _receipt(
    request: VerificationRequest,
    status: VerificationStatus,
    codes: tuple[ReasonCode, ...],
    equality: EqualityResult,
    relations: tuple[RelationIdentity, ...],
    cleanup_complete: bool,
) -> VerificationReceipt:
    plan = request.plan

    def sha(text: str | None) -> str:
        return hashlib.sha256((text or "").encode("utf-8")).hexdigest()

    return VerificationReceipt(
        run_id=request.workspace.run_id,
        model_unique_id=request.baseline.unique_id,
        source_path=Path(request.baseline.original_file_path),
        plan_sha256=sha(f"{plan.original_source_sha256}:{plan.candidate_source_sha256}"),
        original_source_sha256=plan.original_source_sha256,
        candidate_source_sha256=plan.candidate_source_sha256,
        original_compiled_sha256=sha(request.baseline.compiled_code),
        candidate_compiled_sha256=sha(request.candidate.compiled_code),
        dbt_version=request.view.metadata.dbt_version,
        manifest_schema_version=request.view.metadata.dbt_schema_version,
        adapter_type=request.spec.name,
        sqlglot_version=_pkg_version("sqlglot"),
        comparator_version=COMPARATOR_VERSION,
        compilation_context_sha256=sha(request.context.package_state_sha256),
        scratch_relations=relations,
        equality=equality,
        status=status,
        reason_codes=codes,
        warning_codes=(),
        cleanup_complete=cleanup_complete,
    )


def _profile_name(config: AppConfig, project_dir: Path) -> str:
    profile = config.profile or read_project_profile(project_dir)
    if not profile:
        raise VerificationError(
            ReasonCode.DBT_COMMAND_FAILED, "no dbt profile: pass --profile or set it in dbt_project.yml"
        )
    return profile


def _project_profiles_dir(project_dir: Path) -> Path | None:
    """dbt reads ./profiles.yml from the working directory; the harness runs elsewhere, so pass it on."""
    return project_dir if (project_dir / "profiles.yml").is_file() else None


def _harness_target(ws: RunWorkspace) -> Path:
    return ws.artifacts_root / "harness-target"


def _quoted(relation: RelationIdentity) -> str:
    return ".".join(quote_ident(part.value) for part in (relation.database, relation.schema, relation.identifier))


def _tail(res: Any) -> str:
    text: str = (res.stderr or "") + (res.stdout or "")
    return text[-2000:]
