"""Warehouse verification for one model (PostgreSQL), and cleanup of a run's scratch relations.

Flow per model:

1. Validate the scratch schema and write a throwaway dbt project (``harness``) holding the baseline
   and candidate compiled SQL as two views, aliased with a per-run, per-model token.
2. ``dbt parse`` and preflight the manifest: exactly those two views, no hooks, inside the scratch
   schema. Only then record the relations in the ledger and ``dbt run`` them.
3. Read both views' column names and exact types from ``pg_catalog``. Unsupported types refuse;
   a schema difference is DIFFERENT.
4. Compare the two views as bags in ONE statement (one snapshot).
5. Drop the views (views only, by exact name) and confirm they are gone, whatever happened above.

Every query runs through the harness ``dbt_refmerge_query`` macro, so dbt-refmerge needs no database
driver: dbt and the user's profile provide the connection.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from collections.abc import Callable, Sequence
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
    ScratchBoundary,
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
    build_harness_project,
    build_verdict_sql,
    preflight_harness_manifest,
    relation_token,
    scratch_identifiers,
    validate_scratch_schema,
    write_harness_macros,
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


VerifyRunner = Callable[[VerificationRequest], VerificationReceipt]


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

    def run(self) -> None:
        res = self.dbt.run(self.invocation, "fqn:*")
        if res.returncode != 0:
            raise DbtError(f"dbt run failed for the verification harness: {_tail(res)}", argv=res.argv_redacted)

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

    def drop_views(self, schema: str, identifiers: Sequence[str]) -> None:
        res = self.dbt.run_operation(
            self.invocation,
            "dbt_refmerge_drop_views",
            {"schema": schema, "identifiers": list(identifiers)},
        )
        if res.returncode != 0:
            raise DbtError(f"dropping scratch views failed: {_tail(res)}", argv=res.argv_redacted)


def verify_postgres(request: VerificationRequest) -> VerificationReceipt:
    ws = request.workspace
    token = relation_token(ws.run_id, request.baseline.unique_id)
    session: HarnessSession | None = None
    scratch_schema = ""
    relations: tuple[RelationIdentity, ...] = ()
    created = False
    try:
        scratch_schema = validate_scratch_schema(
            request.config.scratch_schema or "", model_schema=request.baseline.schema_
        ).value
        database = request.baseline.database
        if not database:
            raise ScratchBoundaryError(f"model {request.baseline.unique_id} has no database in the manifest")
        if request.spec.capabilities is None:
            raise VerificationError(ReasonCode.UNSUPPORTED_ADAPTER, f"adapter {request.spec.name!r} cannot verify")
        harness = build_harness_project(
            ws,
            profile=_profile_name(request.config, request.context.project_dir),
            baseline_sql=request.baseline.compiled_code or "",
            candidate_sql=request.candidate.compiled_code or "",
            token=token,
        )
        relations = tuple(
            RelationIdentity(IdentifierIdentity(database), IdentifierIdentity(scratch_schema), IdentifierIdentity(a))
            for a in (harness.baseline_alias, harness.candidate_alias)
        )
        session = HarnessSession(request.dbt, ws.harness_project, request.config, scratch_schema, _target(ws, token))
        preflight_harness_manifest(
            session.parse(),
            ScratchBoundary(
                database=IdentifierIdentity(database),
                schema=IdentifierIdentity(scratch_schema),
                allowed_relations=relations,
            ),
        )
        for relation in relations:
            ws.record_object(ScratchObject(relation=relation, kind="view"))
        created = True
        session.run()
        status, codes, equality = _compare(session, relations, request.spec.capabilities.bag_strategy)
    except RefmergeError as exc:
        status = VerificationStatus.ERROR if isinstance(exc, DbtError) else VerificationStatus.UNVERIFIABLE
        codes, equality = (exc.reason_code,), _NO_EQUALITY
    finally:
        cleanup_complete = True
        if created and session is not None:
            cleanup_complete = _drop_run_relations(session, ws, scratch_schema, relations, token)
    return _receipt(request, status, codes, equality, relations if created else (), cleanup_complete)


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
    remaining: list[str] = []
    if names:
        session.drop_views(scratch_schema, names)
        rows = session.query("remaining", catalog_sql(scratch_schema, names)).rows
        remaining = sorted({row[0] for row in rows if row[0] is not None})
    return {
        "run_id": run_id,
        "schema": scratch_schema,
        "dropped": [name for name in names if name not in remaining],
        "remaining": remaining,
        "complete": not remaining,
    }


def _compare(
    session: HarnessSession,
    relations: tuple[RelationIdentity, ...],
    strategy: str,
) -> tuple[VerificationStatus, tuple[ReasonCode, ...], EqualityResult]:
    baseline, candidate = relations
    schema = baseline.schema.value
    catalog = session.query("schema", catalog_sql(schema, [baseline.identifier.value, candidate.identifier.value]))
    payload = _schema_payload(catalog, baseline.identifier.value, candidate.identifier.value)
    baseline_schema = normalize_schema(payload)
    candidate_schema = normalize_schema(RawSchemaPayload(baseline=payload.candidate, candidate=[]))
    for relation_schema in (baseline_schema, candidate_schema):
        if validate_types(relation_schema, POSTGRES_EXACT_TYPES) != (ReasonCode.OK,):
            return VerificationStatus.UNVERIFIABLE, (ReasonCode.UNSUPPORTED_COMPARISON_TYPE,), _NO_EQUALITY
    if not schemas_equal(baseline_schema, payload):
        return VerificationStatus.DIFFERENT, (ReasonCode.SCHEMA_MISMATCH,), _NO_EQUALITY
    verdict = session.query(
        "verdict",
        build_verdict_sql(
            _quoted(baseline),
            _quoted(candidate),
            [column.name for column in baseline_schema.columns],
            strategy,
        ),
    )
    equality = parse_equality_result({"schema_equal": True, **_counts(verdict)})
    status = VerificationStatus(derive_status(equality))
    codes = (ReasonCode.OK,) if status is VerificationStatus.SNAPSHOT_EQUIVALENT else (ReasonCode.BAG_DIFFERENCE,)
    return status, codes, equality


def _drop_run_relations(
    session: HarnessSession,
    ws: RunWorkspace,
    schema: str,
    relations: tuple[RelationIdentity, ...],
    token: str,
) -> bool:
    names = scratch_identifiers(token)
    try:
        session.drop_views(schema, names)
        remaining = {row[0] for row in session.query("remaining", catalog_sql(schema, names)).rows}
    except RefmergeError:
        remaining = set(names)
    for relation in relations:
        state = "cleanup_failed" if relation.identifier.value in remaining else "dropped"
        ws.record_object(ScratchObject(relation=relation, kind="view", state=state))
    return not remaining


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


def _target(ws: RunWorkspace, token: str) -> Path:
    return ws.artifacts_root / f"harness-target-{token}"


def _quoted(relation: RelationIdentity) -> str:
    return ".".join(quote_ident(part.value) for part in (relation.database, relation.schema, relation.identifier))


def _tail(res: Any) -> str:
    text: str = (res.stderr or "") + (res.stdout or "")
    return text[-2000:]
