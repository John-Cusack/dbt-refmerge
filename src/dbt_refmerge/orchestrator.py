"""End-to-end orchestrator: scan / check / fix / cleanup service."""

from __future__ import annotations

import contextlib
import dataclasses
import difflib
import hashlib
import os
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dbt_refmerge.adapters import AdapterSpec, FoldRule, canonical_adapter_name, resolve_adapter
from dbt_refmerge.analyze import (
    Finding,
    FindingStatus,
    QualifiedDuplicateGroup,
    group_imports,
    qualify_group,
)
from dbt_refmerge.artifacts import ManifestNodeModel, ManifestView, load_manifest
from dbt_refmerge.config import AppConfig, CompilationContext, is_secret_name
from dbt_refmerge.dbt_cli import DbtCli, DbtInvocation
from dbt_refmerge.domain import (
    EqualityResult,
    ReasonCode,
    RefCall,
    RewritePlan,
    VerificationReceipt,
    VerificationStatus,
    is_fixable,
)
from dbt_refmerge.errors import (
    ArtifactError,
    CleanupError,
    DbtError,
    RefmergeError,
    SemanticError,
    SourceChangedError,
)
from dbt_refmerge.rewrite import apply_edits, build_plan
from dbt_refmerge.semantics import (
    analyze_volatility,
    build_expected_transform,
    match_source_ctes,
    parse_model,
    semantic_fingerprint,
)
from dbt_refmerge.source import decode_source, mask_jinja, parse_source_model, source_sha256
from dbt_refmerge.verification.runner import VerificationRequest, VerifyRunner, cleanup_run, verify_postgres_batch
from dbt_refmerge.workspace import RunWorkspace


@dataclass(frozen=True)
class ScanRequest:
    config: AppConfig


@dataclass(frozen=True)
class CheckRequest:
    """``select`` is a dbt selector; ``model_path`` (project-relative) checks exactly that model file."""

    config: AppConfig
    select: str | None = None
    model_path: Path | None = None


@dataclass(frozen=True)
class FixRequest:
    config: AppConfig
    model_path: Path
    dry_run: bool = False


@dataclass(frozen=True)
class CleanupRequest:
    config: AppConfig
    run_id: str


@dataclass(frozen=True)
class ModelResult:
    model_unique_id: str
    source_path: Path
    receipt: VerificationReceipt
    diff: str


@dataclass(frozen=True)
class ScanReport:
    findings: tuple[Finding, ...]


@dataclass(frozen=True)
class CheckReport:
    run_id: str
    results: tuple[ModelResult, ...]
    candidate_bytes_map: dict[str, bytes]
    dbt_version: str = ""
    manifest_schema_version: str = ""
    workspace_root: Path | None = None  # set when the workspace is kept


@dataclass(frozen=True)
class FixReport:
    applied: bool
    dry_run: bool
    result: ModelResult | None
    reason: str = ""


def _model_paths(project_dir: Path) -> list[str]:
    """``model-paths`` from dbt_project.yml (dbt's default ``models``); unusable values fall back to the default."""
    import yaml

    for filename in ("dbt_project.yml", "dbt_project.yaml"):
        path = project_dir / filename
        if not path.is_file():
            continue
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, yaml.YAMLError):
            break
        paths = raw.get("model-paths") if isinstance(raw, dict) else None
        if isinstance(paths, list) and paths and all(isinstance(p, str) for p in paths):
            return paths
        break
    return ["models"]


def discover_model_files(project_dir: Path) -> list[Path]:
    """SQL files under the project's model paths. Paths escaping the project are ignored."""
    root = project_dir.resolve()
    found: set[Path] = set()
    for rel in _model_paths(project_dir):
        base = project_dir / rel
        if not base.is_dir() or not base.resolve().is_relative_to(root):
            continue
        found.update(p for p in base.rglob("*.sql") if p.is_file())
    return sorted(found)


def project_relative_path(model_path: Path, project_dir: Path) -> Path | None:
    """``model_path`` (absolute, relative to the working directory, or relative to the project) as a
    project-relative path, or None when it is not inside the project."""
    root = project_dir.resolve()
    candidates = [model_path] if model_path.is_absolute() else [Path.cwd() / model_path, root / model_path]
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved.is_relative_to(root) and (resolved.is_file() or candidate is candidates[-1]):
            return resolved.relative_to(root)
    return None


def _line_of(source: bytes, byte_offset: int) -> int:
    return source.count(b"\n", 0, byte_offset) + 1


def _first_duplicate_calls(calls: Iterable[RefCall]) -> list[RefCall] | None:
    """The literal ref()/source() calls that may name one relation, for the relation named first.

    Calls are grouped by name alone: ``ref('stg')`` and ``ref('pkg', 'stg')`` can resolve to the same model.
    """
    grouped: dict[tuple[Any, ...], list[RefCall]] = {}
    for call in calls:
        grouped.setdefault((call.kind, call.name, call.source_name), []).append(call)
    duplicates = [group for group in grouped.values() if len(group) >= 2]
    return min(duplicates, key=lambda group: group[0].span.start_byte) if duplicates else None


def _literal_calls(source: bytes) -> tuple[RefCall, ...]:
    """Every literal ref()/source() call in the model, even when its SQL spells a reserved sentinel name."""
    try:
        return tuple(mask_jinja(decode_source(source), reject_sentinel_names=False).ref_calls.values())
    except RefmergeError:
        return ()  # not UTF-8 or unterminated Jinja: there is no import to merge


def _may_import_twice(source: bytes) -> bool:
    """Whether two literal ref()/source() calls in the model may name the same relation."""
    return _first_duplicate_calls(_literal_calls(source)) is not None


def detect_source_duplicates(
    source: bytes,
    manifest: ManifestView | None,
    model_uid: str,
    source_path: Path,
    fold_unquoted: FoldRule = "lower",
) -> Finding | None:
    def unsupported(calls: list[RefCall]) -> Finding:
        return Finding(
            model_unique_id=model_uid,
            source_path=source_path,
            upstream_unique_id="",
            cte_names=(),
            status=FindingStatus.NEEDS_COMPILED_ANALYSIS,
            reason_codes=(ReasonCode.UNSUPPORTED_IMPORT_SHAPE,),
            line=_line_of(source, calls[0].span.start_byte),
        )

    try:
        model = parse_source_model(source, fold_unquoted=fold_unquoted)
    except RefmergeError:
        # A model the CTE parser cannot read is a lead only if it may name one relation in two literal calls.
        duplicate_calls = _first_duplicate_calls(_literal_calls(source))
        return unsupported(duplicate_calls) if duplicate_calls else None
    from dbt_refmerge.source import has_unsupported_duplicate_candidates

    if has_unsupported_duplicate_candidates(model):
        duplicate_calls = _first_duplicate_calls(model.masked.ref_calls.values())
        assert duplicate_calls is not None  # two CTEs import one relation, so two literal calls name it
        return unsupported(duplicate_calls)
    import_ctes = [c for c in model.ctes if c.ref_call is not None]
    if len(import_ctes) < 2:
        return None
    # group by (kind, package, name, source_name, version) literal identity as static proxy
    from collections import defaultdict

    grouped: dict[tuple[Any, ...], list[Any]] = defaultdict(list)
    for c in import_ctes:
        rc = c.ref_call
        assert rc is not None
        grouped[(rc.kind, rc.package, rc.name, rc.source_name, rc.version)].append(c)
    dup = [v for v in grouped.values() if len(v) >= 2]
    if not dup:
        return None
    # report first group only in scan (compiled analysis refines)
    first = sorted(dup, key=lambda g: min(c.ordinal for c in g))[0]
    upstream = ""
    if manifest is not None:
        owner = manifest.nodes.get(model_uid)
        if owner is not None:
            from dbt_refmerge.artifacts import resolve_literal_ref

            try:
                rc0 = first[0].ref_call
                assert rc0 is not None
                upstream = resolve_literal_ref(manifest, owner, rc0.kind, rc0.package, rc0.name, rc0.source_name)
            except RefmergeError:
                upstream = ""
    return Finding(
        model_unique_id=model_uid,
        source_path=source_path,
        upstream_unique_id=upstream,
        cte_names=tuple(c.identifier.source_text for c in sorted(first, key=lambda c: c.ordinal)),
        status=FindingStatus.NEEDS_COMPILED_ANALYSIS,
        reason_codes=(ReasonCode.NEEDS_COMPILED_ANALYSIS,),
        line=_line_of(source, min(c.cte_span.start_byte for c in first)),
    )


class RefmergeService:
    def __init__(
        self,
        dbt_factory: Callable[[AppConfig], DbtCli] | None = None,
        verify_runner: VerifyRunner | None = None,
        progress: Callable[[str], None] | None = None,
    ) -> None:
        self._dbt_factory = dbt_factory or (lambda cfg: DbtCli(tuple(cfg.dbt_command)))
        self._verify_runner = verify_runner or verify_postgres_batch
        self._progress = progress or (lambda message: None)

    # -- scan --
    def scan(self, request: ScanRequest) -> ScanReport:
        project_dir = request.config.project_dir
        files = discover_model_files(project_dir)
        manifest: ManifestView | None = None
        target_manifest = project_dir / "target" / "manifest.json"
        if target_manifest.is_file():
            try:
                manifest = load_manifest(target_manifest)
            except RefmergeError:
                manifest = None
        spec = resolve_adapter(
            cli_override=request.config.adapter,
            manifest_adapter=manifest.metadata.adapter_type if manifest is not None else None,
            project_dir=project_dir,
            profiles_dir=request.config.profiles_dir,
            profile=request.config.profile,
            target=request.config.target,
        )
        nodes_by_path = {
            node.original_file_path.replace("\\", "/"): node.unique_id
            for node in (_project_models(manifest) if manifest is not None else [])
        }
        findings: list[Finding] = []
        for path in sorted(files):
            data = path.read_bytes()
            rel = path.relative_to(project_dir).as_posix()
            uid = nodes_by_path.get(rel, f"model.{path.stem}")
            finding = detect_source_duplicates(data, manifest, uid, path, spec.fold_unquoted)
            if finding is not None:
                findings.append(finding)
        findings.sort(key=lambda f: (str(f.source_path), f.model_unique_id, f.upstream_unique_id))
        return ScanReport(findings=tuple(findings))

    # -- check --
    def check(self, request: CheckRequest) -> CheckReport:
        config = request.config
        if not config.scratch_schema:
            raise RefmergeError(ReasonCode.SCRATCH_BOUNDARY_VIOLATION, "scratch_schema required")
        spec = resolve_adapter(
            cli_override=config.adapter,
            manifest_adapter=None,
            project_dir=config.project_dir,
            profiles_dir=config.profiles_dir,
            profile=config.profile,
            target=config.target,
        )
        if not spec.verifies:
            raise RefmergeError(
                ReasonCode.UNSUPPORTED_ADAPTER,
                f"adapter {spec.name!r} is known but warehouse verification is unverified in v0.1 "
                "(supports: postgres); scan remains available",
            )
        ws = RunWorkspace.create(keep=config.keep_workspace)
        try:
            self._progress("copying the project into a private workspace")
            snapshot = ws.snapshot_project(config.project_dir)
            dbt = self._dbt_factory(config)
            version = dbt.version(cwd=snapshot.root)
            caps = dbt.discover_capabilities(cwd=snapshot.root)
            context = CompilationContext(
                dbt_executable=Path(config.dbt_command[0]),
                dbt_version_text=version.raw,
                project_dir=snapshot.root,
                profiles_dir=config.profiles_dir,
                profile=config.profile or "",
                target=config.target,
                vars_json=None,
                passthrough_args=(),
                environment_names=tuple(sorted([k for k in os.environ if not is_secret_name(k)])),
                package_state_sha256=ws.package_state_digest(snapshot.root),
                adapter=spec.name,
            )
            _ = caps
            if request.model_path is not None:
                selector = f"path:{request.model_path.as_posix()}"
            else:
                selector = request.select or "fqn:*"
            baseline_inv = DbtInvocation(
                project_dir=snapshot.root,
                profiles_dir=config.profiles_dir,
                profile=config.profile,
                target=config.target,
                target_path=ws.artifacts_root / "baseline-target",
                threads=1,
                timeout_seconds=config.subprocess_timeout_seconds,
            )
            self._progress(f"compiling the project with dbt (--select {selector})")
            res = dbt.compile(baseline_inv, selector)
            if res.returncode != 0:
                raise DbtError(f"dbt compile failed: {res.stderr[-2000:]}", argv=res.argv_redacted)
            # Every supported dbt honors --target-path; a stale <project>/target manifest is never read instead.
            view = load_manifest(ws.artifacts_root / "baseline-target" / "manifest.json")
            _require_manifest_adapter(view, spec)
            # dbt compiled exactly the selected nodes; package models are not the project's to rewrite.
            models = [n for n in _project_models(view) if n.compiled_code is not None]
            if request.model_path is not None:
                models = [n for n in models if n.original_file_path.replace("\\", "/") == request.model_path.as_posix()]
            elif not models:
                raise RefmergeError(ReasonCode.INTERNAL_ERROR, f"no models selected by {selector!r}")
            results: dict[str, ModelResult] = {}
            pending: list[_PendingMerge] = []
            self._progress(f"analyzing {len(models)} model{'s' if len(models) != 1 else ''}")
            for node in sorted(models, key=lambda n: n.unique_id):
                outcome = self._prepare_model(ws, context, view, node, spec)
                if isinstance(outcome, ModelResult):
                    results[node.unique_id] = outcome
                else:
                    pending.append(outcome)
            verifiable = self._compile_candidates(config, ws, dbt, context, view, spec, pending, results)
            cand_map = dict.fromkeys(results, b"")
            if verifiable:
                self._progress(
                    f"verifying {len(verifiable)} merge{'s' if len(verifiable) != 1 else ''} on the warehouse "
                    f"(scratch schema {config.scratch_schema}; views are dropped afterwards)"
                )
                requests = [
                    VerificationRequest(
                        config=config,
                        workspace=ws,
                        dbt=dbt,
                        context=context,
                        view=view,
                        baseline=merge.node,
                        candidate=candidate,
                        plan=merge.plan,
                        spec=spec,
                    )
                    for merge, candidate in verifiable
                ]
                for (merge, _candidate), receipt in zip(verifiable, self._verify_runner(requests), strict=True):
                    diff = _unified_diff(merge.raw, merge.candidate_bytes, merge.rel.as_posix())
                    results[merge.node.unique_id] = ModelResult(merge.node.unique_id, merge.src_path, receipt, diff)
                    cand_map[merge.node.unique_id] = merge.candidate_bytes
            return CheckReport(
                run_id=ws.run_id,
                results=tuple(results[uid] for uid in sorted(results)),
                candidate_bytes_map=cand_map,
                dbt_version=view.metadata.dbt_version,
                manifest_schema_version=view.metadata.dbt_schema_version,
                workspace_root=ws.root if config.keep_workspace else None,
            )
        finally:
            if not config.keep_workspace:
                # A leftover local temp directory must not replace the report or the real error.
                with contextlib.suppress(CleanupError):
                    ws.cleanup_files()

    def _prepare_model(
        self,
        ws: RunWorkspace,
        context: CompilationContext,
        view: ManifestView,
        node: ManifestNodeModel,
        spec: AdapterSpec,
    ) -> ModelResult | _PendingMerge:
        """Static analysis and rewrite for one model: a final refusal, or a candidate written for compilation."""
        from dbt_refmerge.source import parse_source_model as _parse_src

        src_path = ws.source_snapshot / node.original_file_path
        # The rewrite edits this exact file; never substitute a same-named file or the manifest's raw_code.
        raw = src_path.read_bytes() if src_path.is_file() else None

        def refuse(codes: tuple[ReasonCode, ...], path: Path) -> ModelResult:
            # A model that cannot be analyzed is only worth reporting when it may import a relation twice;
            # otherwise every incremental or unparseable model would fail `check`.
            evidence = raw if raw is not None else node.raw_code.encode("utf-8")
            if not _may_import_twice(evidence):
                return ModelResult(node.unique_id, path, _ok_noop_receipt(ws, context, view, node, evidence), "")
            return ModelResult(node.unique_id, path, _unverifiable_receipt(ws, context, view, node, codes), "")

        if node.config.get("materialized", "view") not in ("table", "view"):
            return refuse((ReasonCode.UNSUPPORTED_MODEL_TYPE,), Path(node.original_file_path))
        if raw is None:
            return refuse((ReasonCode.SOURCE_MAPPING_AMBIGUOUS,), Path(node.original_file_path))
        try:
            parsed_src = _parse_src(raw, fold_unquoted=spec.fold_unquoted)
        except RefmergeError as exc:
            # One model the source frontend refuses must not abort the rest of the run.
            return refuse((exc.reason_code,), src_path)
        from dbt_refmerge.source import has_unsupported_duplicate_candidates

        if has_unsupported_duplicate_candidates(parsed_src):
            return refuse((ReasonCode.UNSUPPORTED_IMPORT_SHAPE,), src_path)
        try:
            matched = match_source_ctes(
                tuple(c for c in parsed_src.ctes if c.ref_call is not None),
                parse_model(node.compiled_code or "", spec.sqlglot_dialect),
                view,
                node,
            )
        except (SemanticError, ArtifactError) as exc:
            return refuse((exc.reason_code,), src_path)
        groups = group_imports(list(matched), node.unique_id)
        if not groups:
            return ModelResult(node.unique_id, src_path, _ok_noop_receipt(ws, context, view, node, raw), "")
        # volatility gate on whole model
        parsed_compiled = parse_model(node.compiled_code or "", spec.sqlglot_dialect)
        whole_ok = analyze_volatility(parsed_compiled).ok
        qualified = tuple(
            qualify_group(
                g,
                whole_model_ok=whole_ok,
                downstream_refs=parsed_src.downstream_refs,
                nested_names=parsed_src.nested_names,
            )
            for g in groups
        )
        eligible = tuple(q for q in qualified if q.status == FindingStatus.MERGE_ELIGIBLE)
        if not eligible:
            return refuse(qualified[0].reason_codes, src_path)
        try:
            plan = build_plan(raw, node.unique_id, src_path, eligible, parsed_src)
        except RefmergeError as exc:
            return refuse((exc.reason_code,), src_path)
        candidate_bytes = apply_edits(raw, plan.edits)
        rel = Path(node.original_file_path)
        (ws.candidate_project / rel).parent.mkdir(parents=True, exist_ok=True)
        (ws.candidate_project / rel).write_bytes(candidate_bytes)
        return _PendingMerge(node, src_path, rel, raw, plan, candidate_bytes, eligible)

    def _compile_candidates(
        self,
        config: AppConfig,
        ws: RunWorkspace,
        dbt: DbtCli,
        context: CompilationContext,
        view: ManifestView,
        spec: AdapterSpec,
        pending: list[_PendingMerge],
        results: dict[str, ModelResult],
    ) -> list[tuple[_PendingMerge, ManifestNodeModel]]:
        """Compile every candidate in one dbt call and apply the compiled-delta gate.

        Refusals go into ``results``. A failed batch compile is retried one candidate at a time so the
        failure is attributed to the models that caused it (dbt stops at parse errors without per-node results).
        """
        if not pending:
            return []
        self._progress(f"compiling {len(pending)} candidate merge{'s' if len(pending) != 1 else ''}")
        invocation = DbtInvocation(
            project_dir=ws.candidate_project,
            profiles_dir=config.profiles_dir,
            profile=config.profile,
            target=config.target,
            target_path=ws.artifacts_root / "candidate-target",
            threads=1,
            timeout_seconds=config.subprocess_timeout_seconds,
        )
        compiled = _compiled_candidates(dbt, invocation, pending)
        if compiled is None:
            # dbt parses the whole project, so a broken candidate fails every compile it is present in: each retry
            # compiles one candidate with the other models restored to their original source.
            compiled = {}
            for merge in pending:
                (ws.candidate_project / merge.rel).write_bytes(merge.raw)
            for index, merge in enumerate(pending):
                (ws.candidate_project / merge.rel).write_bytes(merge.candidate_bytes)
                single = dataclasses.replace(invocation, target_path=ws.artifacts_root / f"candidate-target-{index}")
                compiled.update(_compiled_candidates(dbt, single, [merge]) or {})
                (ws.candidate_project / merge.rel).write_bytes(merge.raw)
        verifiable: list[tuple[_PendingMerge, ManifestNodeModel]] = []
        for merge in pending:
            candidate = compiled[merge.node.unique_id]
            if isinstance(candidate, ReasonCode):
                codes: tuple[ReasonCode, ...] = (candidate,)
            else:
                try:
                    self._validate_delta(merge.node, candidate, merge.eligible, spec)
                except SemanticError as exc:
                    codes = (exc.reason_code,)
                else:
                    verifiable.append((merge, candidate))
                    continue
            receipt = _unverifiable_receipt(ws, context, view, merge.node, codes)
            results[merge.node.unique_id] = ModelResult(merge.node.unique_id, merge.src_path, receipt, "")
        return verifiable

    def _validate_delta(
        self,
        baseline: ManifestNodeModel,
        candidate: ManifestNodeModel,
        eligible: tuple[QualifiedDuplicateGroup, ...],
        spec: AdapterSpec,
    ) -> None:
        from dbt_refmerge.semantics import ParsedModel, validate_compiled_delta

        bparsed = parse_model(baseline.compiled_code or "", spec.sqlglot_dialect)
        cparsed = parse_model(candidate.compiled_code or "", spec.sqlglot_dialect)
        # The candidate merges every eligible group at once, so apply all of them before comparing.
        expected = bparsed
        for qg in eligible:
            members = sorted(qg.group.imports, key=lambda m: m.source_cte.ordinal)
            canon_ident = members[0].semantic.cte_identity.value
            donor_idents = tuple(m.semantic.cte_identity.value for m in members[1:])
            # Added projections, in the rewrite's order, rebuilt from the donor's own compiled SQL so
            # aliases and quoting survive (the folded output identity is not SQL).
            have = {p.output_identity.value for p in members[0].semantic.projections}
            additions: list[str] = []
            for m in members[1:]:
                donor_select = bparsed.ctes[m.semantic.cte_identity.value].args["this"]
                for projection, node in zip(m.semantic.projections, donor_select.expressions, strict=True):
                    if projection.output_identity.value not in have:
                        have.add(projection.output_identity.value)
                        additions.append(node.sql(dialect=spec.sqlglot_dialect))
            expected_tree = build_expected_transform(expected, canon_ident, donor_idents, {canon_ident: additions})
            expected = ParsedModel(sql=bparsed.sql, dialect=bparsed.dialect, tree=expected_tree, ctes={})
        validate_compiled_delta(bparsed, cparsed, semantic_fingerprint(expected.tree, fold=spec.fold_unquoted))

    # -- fix --
    def fix(self, request: FixRequest) -> FixReport:
        rel = project_relative_path(request.model_path, request.config.project_dir)
        if rel is None:
            return FixReport(applied=False, dry_run=request.dry_run, result=None, reason="model not found")
        check_report = self.check(CheckRequest(config=request.config, model_path=rel))
        target = next((r for r in check_report.results if r.receipt.source_path.as_posix() == rel.as_posix()), None)
        if target is None:
            return FixReport(applied=False, dry_run=request.dry_run, result=None, reason="model not found")
        if not is_fixable(target.receipt):
            return FixReport(applied=False, dry_run=request.dry_run, result=target, reason="not fixable")
        candidate_bytes = check_report.candidate_bytes_map.get(target.model_unique_id, b"")
        if request.dry_run:
            return FixReport(applied=False, dry_run=True, result=target, reason="dry-run")
        live_path = request.config.project_dir / rel
        self._progress(f"writing the verified merge to {rel.as_posix()}")
        apply_verified_source(
            live_path,
            expected_original_sha256=target.receipt.original_source_sha256,
            candidate_bytes=candidate_bytes,
            expected_candidate_sha256=target.receipt.candidate_source_sha256,
        )
        return FixReport(applied=True, dry_run=False, result=target, reason="applied")

    def cleanup(self, request: CleanupRequest) -> dict[str, Any]:
        """Drop the scratch views a check run left behind, found by run id in the scratch schema."""
        ws = RunWorkspace.create(keep=False)
        try:
            return cleanup_run(request.config, self._dbt_factory(request.config), request.run_id, ws)
        finally:
            ws.cleanup_files()


def _require_manifest_adapter(view: ManifestView, spec: AdapterSpec) -> None:
    """Fail closed when the compiled manifest targets a different warehouse."""
    adapter_type = view.metadata.adapter_type
    if adapter_type is None:
        return  # old artifact without adapter metadata; resolved spec already governs
    if canonical_adapter_name(adapter_type) != spec.name:
        raise RefmergeError(
            ReasonCode.ADAPTER_MISMATCH,
            f"manifest adapter {adapter_type!r} does not match resolved adapter {spec.name!r}; "
            "recompile for the selected target",
        )


@dataclass(frozen=True)
class _PendingMerge:
    """A model whose rewrite is written to the candidate project and awaits compilation and verification."""

    node: ManifestNodeModel
    src_path: Path
    rel: Path
    raw: bytes
    plan: RewritePlan
    candidate_bytes: bytes
    eligible: tuple[QualifiedDuplicateGroup, ...]


def _compiled_candidates(
    dbt: DbtCli, invocation: DbtInvocation, pending: list[_PendingMerge]
) -> dict[str, ManifestNodeModel | ReasonCode] | None:
    """Each pending model's compiled candidate node or refusal code.

    None when a compile of several candidates failed, since dbt does not say which one broke it.
    """
    selector = " ".join(f"path:{merge.rel.as_posix()}" for merge in pending)
    result = dbt.compile(invocation, selector)
    if result.returncode != 0:
        return None if len(pending) > 1 else {pending[0].node.unique_id: ReasonCode.DBT_COMMAND_FAILED}
    try:
        view = load_manifest(Path(str(invocation.target_path)) / "manifest.json")
    except ArtifactError as exc:
        return {merge.node.unique_id: exc.reason_code for merge in pending}
    outcome: dict[str, ManifestNodeModel | ReasonCode] = {}
    for merge in pending:
        node = view.get(merge.node.unique_id)
        outcome[merge.node.unique_id] = node if node is not None and node.compiled_code else ReasonCode.COMPILE_DRIFT
    return outcome


def _project_models(view: ManifestView) -> list[ManifestNodeModel]:
    """Models of the root project (not installed packages) when the manifest names the project."""
    project = view.metadata.project_name
    return [n for n in view.models() if project is None or n.package_name == project]


def _unified_diff(a: bytes, b: bytes, filename: str) -> str:
    try:
        a_text = a.decode("utf-8").splitlines(keepends=True)
        b_text = b.decode("utf-8").splitlines(keepends=True)
    except UnicodeDecodeError:
        return ""
    return "".join(difflib.unified_diff(a_text, b_text, fromfile=f"a/{filename}", tofile=f"b/{filename}"))


def _base_receipt(
    ws: RunWorkspace,
    context: CompilationContext,
    view: ManifestView,
    node: ManifestNodeModel,
    raw: bytes,
    status: VerificationStatus,
    codes: tuple[ReasonCode, ...],
    equality: EqualityResult,
) -> VerificationReceipt:
    from importlib.metadata import version as _pkg_version

    return VerificationReceipt(
        run_id=ws.run_id,
        model_unique_id=node.unique_id,
        source_path=Path(node.original_file_path),
        plan_sha256=hashlib.sha256(node.unique_id.encode()).hexdigest(),
        original_source_sha256=source_sha256(raw),
        candidate_source_sha256=source_sha256(raw),
        original_compiled_sha256=hashlib.sha256((node.compiled_code or "").encode()).hexdigest(),
        candidate_compiled_sha256=hashlib.sha256((node.compiled_code or "").encode()).hexdigest(),
        dbt_version=view.metadata.dbt_version,
        manifest_schema_version=view.metadata.dbt_schema_version,
        adapter_type=context.adapter,
        sqlglot_version=_pkg_version("sqlglot"),
        comparator_version="1",
        compilation_context_sha256=hashlib.sha256(context.package_state_sha256.encode()).hexdigest(),
        scratch_relations=(),
        equality=equality,
        status=status,
        reason_codes=codes,
        warning_codes=(),
        cleanup_complete=True,
    )


def _unverifiable_receipt(
    ws: RunWorkspace,
    context: CompilationContext,
    view: ManifestView,
    node: ManifestNodeModel,
    codes: tuple[ReasonCode, ...],
) -> VerificationReceipt:
    raw = node.raw_code.encode("utf-8")
    return _base_receipt(
        ws,
        context,
        view,
        node,
        raw,
        VerificationStatus.UNVERIFIABLE,
        tuple(codes),
        EqualityResult(False, 0, 0, 0, 0),
    )


def _ok_noop_receipt(
    ws: RunWorkspace,
    context: CompilationContext,
    view: ManifestView,
    node: ManifestNodeModel,
    raw: bytes,
) -> VerificationReceipt:
    return _base_receipt(
        ws,
        context,
        view,
        node,
        raw,
        VerificationStatus.NOT_RUN,
        (ReasonCode.NO_DUPLICATE_IMPORT,),
        EqualityResult(True, 0, 0, 0, 0),
    )


def apply_verified_source(
    path: Path,
    *,
    expected_original_sha256: str,
    candidate_bytes: bytes,
    expected_candidate_sha256: str,
) -> None:
    """Atomic same-directory replacement under an exclusive lock, with content preconditions.

    The lock file is opened without following symlinks and removed afterwards. The source is
    re-hashed immediately before ``os.replace``; a stat comparison cannot see a same-size edit
    inside the filesystem's mtime granularity.
    """
    import tempfile

    if path.is_symlink() or not path.is_file():
        raise SourceChangedError(ReasonCode.SOURCE_CHANGED_BEFORE_APPLY, f"refusing non-regular file: {path}")
    if hashlib.sha256(candidate_bytes).hexdigest() != expected_candidate_sha256:
        raise SourceChangedError(ReasonCode.SOURCE_CHANGED_BEFORE_APPLY, "candidate digest mismatch")
    lock_path = path.with_name(path.name + ".dbt-refmerge.lock")
    if lock_path.is_symlink():
        raise RefmergeError(ReasonCode.INTERNAL_ERROR, f"refusing symlinked lock file: {lock_path}")
    try:
        lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except OSError as exc:
        raise RefmergeError(ReasonCode.INTERNAL_ERROR, f"cannot open lock file {lock_path}: {exc}") from exc
    try:
        if sys.platform != "win32":  # pragma: win32 no cover
            import fcntl

            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
            except OSError as exc:
                raise RefmergeError(ReasonCode.INTERNAL_ERROR, f"cannot lock {path}: {exc}") from exc
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected_original_sha256:
            raise SourceChangedError(ReasonCode.SOURCE_CHANGED_BEFORE_APPLY, "source changed before apply")
        mode = path.stat().st_mode
        tmp_fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".dbt-refmerge-")
        replaced = False
        try:
            with os.fdopen(tmp_fd, "wb") as fh:
                fh.write(candidate_bytes)
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp_name, mode)
            if hashlib.sha256(path.read_bytes()).hexdigest() != expected_original_sha256:
                raise SourceChangedError(ReasonCode.SOURCE_CHANGED_BEFORE_APPLY, "source changed before apply")
            os.replace(tmp_name, path)
            replaced = True
            if sys.platform != "win32":  # pragma: win32 no cover
                # Best effort: the rename is done, so a failure here must not report the apply as failed.
                try:
                    dir_fd = os.open(str(path.parent), os.O_DIRECTORY)
                    try:
                        os.fsync(dir_fd)
                    finally:
                        os.close(dir_fd)
                except OSError:
                    pass
        finally:
            if not replaced:
                _unlink_quietly(Path(tmp_name))  # never mask the error that stopped the apply
    finally:
        if sys.platform != "win32":  # pragma: win32 no cover
            # Unlink while still holding the lock; a waiter on the old inode re-hashes and refuses.
            _unlink_quietly(lock_path)
        os.close(lock_fd)
        if sys.platform == "win32":  # pragma: win32 cover
            _unlink_quietly(lock_path)  # Windows cannot delete a file that is still open


def _unlink_quietly(path: Path) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass
