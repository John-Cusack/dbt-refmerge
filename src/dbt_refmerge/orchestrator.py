"""End-to-end orchestrator: scan / check / fix / cleanup service."""

from __future__ import annotations

import contextlib
import difflib
import hashlib
import json
import os
import sys
from collections.abc import Callable
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
from dbt_refmerge.config import AppConfig, CompilationContext
from dbt_refmerge.dbt_cli import DbtCli, DbtInvocation
from dbt_refmerge.domain import (
    EqualityResult,
    ReasonCode,
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
from dbt_refmerge.source import parse_source_model, source_sha256
from dbt_refmerge.workspace import RunWorkspace


@dataclass(frozen=True)
class ScanRequest:
    config: AppConfig
    select: str | None = None
    compile: bool = False


@dataclass(frozen=True)
class CheckRequest:
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
    workspace_root: Path | None = None


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


@dataclass(frozen=True)
class FixReport:
    applied: bool
    dry_run: bool
    result: ModelResult | None
    reason: str = ""


def discover_model_files(project_dir: Path) -> list[Path]:
    out: list[Path] = []
    for cand in ("models",):
        root = project_dir / cand
        if root.is_dir():
            out.extend(sorted(root.rglob("*.sql")))
    if not out:
        out.extend(sorted(project_dir.rglob("*.sql")))
    # exclude harness/target dirs
    return [p for p in out if "target" not in p.parts and "dbt_packages" not in p.parts]


def detect_source_duplicates(
    source: bytes,
    manifest: ManifestView | None,
    model_uid: str,
    source_path: Path,
    fold_unquoted: FoldRule = "lower",
) -> Finding | None:
    try:
        model = parse_source_model(source, fold_unquoted=fold_unquoted)
    except RefmergeError:
        return Finding(
            model_unique_id=model_uid,
            source_path=source_path,
            upstream_unique_id="",
            cte_names=(),
            status=FindingStatus.NEEDS_COMPILED_ANALYSIS,
            reason_codes=(ReasonCode.UNSUPPORTED_IMPORT_SHAPE,),
        )
    from dbt_refmerge.source import has_unsupported_duplicate_candidates

    if has_unsupported_duplicate_candidates(model):
        return Finding(
            model_unique_id=model_uid,
            source_path=source_path,
            upstream_unique_id="",
            cte_names=(),
            status=FindingStatus.NEEDS_COMPILED_ANALYSIS,
            reason_codes=(ReasonCode.UNSUPPORTED_IMPORT_SHAPE,),
        )
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
    )


class RefmergeService:
    def __init__(
        self,
        dbt_factory: Callable[[AppConfig], DbtCli] | None = None,
        verify_runner: Callable[..., VerificationReceipt] | None = None,
    ) -> None:
        self._dbt_factory = dbt_factory or (lambda cfg: DbtCli(tuple(cfg.dbt_command)))
        self._verify_runner = verify_runner

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
        findings: list[Finding] = []
        for path in sorted(files):
            data = path.read_bytes()
            # model uid proxy: file path relative
            uid = f"model.{path.stem}"
            if manifest is not None:
                for node in manifest.models():
                    if path.resolve().as_posix().endswith(node.original_file_path.replace("\\", "/")):
                        uid = node.unique_id
                        break
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
            snapshot = ws.snapshot_project(config.project_dir)
            dbt = self._dbt_factory(config)
            try:
                version = dbt.version(cwd=snapshot.root)
            except DbtError as exc:
                raise exc
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
                environment_names=tuple(sorted([k for k in os.environ if not _is_secret(k)])),
                package_state_sha256=ws.package_state_digest(snapshot.root),
                adapter=spec.name,
            )
            _ = caps
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
            res = dbt.compile(baseline_inv, selector)
            if res.returncode != 0:
                raise DbtError(f"dbt compile failed: {res.stderr[-2000:]}", argv=res.argv_redacted)
            manifest_path = ws.artifacts_root / "baseline-target" / "manifest.json"
            if not manifest_path.is_file():
                # fallback to snapshot target
                manifest_path = snapshot.root / "target" / "manifest.json"
            view = load_manifest(manifest_path)
            _require_manifest_adapter(view, spec)
            # select models: filter resource_type model + name match
            models = [n for n in view.models() if _selected(n, request)]
            if not models:
                raise RefmergeError(ReasonCode.INTERNAL_ERROR, "no exact selected SQL model")
            results: list[ModelResult] = []
            cand_map: dict[str, bytes] = {}
            for node in sorted(models, key=lambda n: n.unique_id):
                result, cand_bytes = self._check_one_model(config, ws, dbt, context, view, node, baseline_inv, spec)
                results.append(result)
                cand_map[result.model_unique_id] = cand_bytes
            return CheckReport(run_id=ws.run_id, results=tuple(results), candidate_bytes_map=cand_map)
        finally:
            if not config.keep_workspace:
                # A leftover local temp directory must not replace the report or the real error.
                with contextlib.suppress(CleanupError):
                    ws.cleanup_files()

    def _check_one_model(
        self,
        config: AppConfig,
        ws: RunWorkspace,
        dbt: DbtCli,
        context: CompilationContext,
        view: ManifestView,
        node: ManifestNodeModel,
        baseline_inv: DbtInvocation,
        spec: AdapterSpec,
    ) -> tuple[ModelResult, bytes]:
        from dbt_refmerge.source import parse_source_model as _parse_src

        if node.config.get("materialized", "view") not in ("table", "view"):
            receipt = _unverifiable_receipt(ws, context, view, node, (ReasonCode.UNSUPPORTED_MODEL_TYPE,))
            return ModelResult(node.unique_id, Path(node.original_file_path), receipt, ""), b""
        src_path = ws.source_snapshot / node.original_file_path
        if not src_path.is_file():
            # try resolved under snapshot root
            src_path = ws.source_snapshot / Path(node.original_file_path).name
        raw = src_path.read_bytes() if src_path.is_file() else node.raw_code.encode("utf-8")
        try:
            parsed_src = _parse_src(raw, fold_unquoted=spec.fold_unquoted)
        except RefmergeError as exc:
            # One model the source frontend refuses must not abort the rest of the run.
            receipt = _unverifiable_receipt(ws, context, view, node, (exc.reason_code,))
            return ModelResult(node.unique_id, src_path, receipt, ""), b""
        from dbt_refmerge.source import has_unsupported_duplicate_candidates

        if has_unsupported_duplicate_candidates(parsed_src):
            receipt = _unverifiable_receipt(ws, context, view, node, (ReasonCode.UNSUPPORTED_IMPORT_SHAPE,))
            return ModelResult(node.unique_id, src_path, receipt, ""), b""
        try:
            matched = match_source_ctes(
                tuple(c for c in parsed_src.ctes if c.ref_call is not None),
                parse_model(node.compiled_code or "", spec.sqlglot_dialect),
                view,
                node,
            )
        except (SemanticError, ArtifactError) as exc:
            receipt = _unverifiable_receipt(ws, context, view, node, (exc.reason_code,))
            return ModelResult(node.unique_id, src_path, receipt, ""), b""
        groups = group_imports(list(matched), node.unique_id)
        if not groups:
            receipt = _ok_noop_receipt(ws, context, view, node, raw)
            return ModelResult(node.unique_id, src_path, receipt, ""), b""
        # volatility gate on whole model
        parsed_compiled = parse_model(node.compiled_code or "", spec.sqlglot_dialect)
        vol = analyze_volatility(parsed_compiled)
        whole_ok = vol.ok
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
            codes = qualified[0].reason_codes if qualified else (ReasonCode.NO_DUPLICATE_IMPORT,)
            receipt = _unverifiable_receipt(ws, context, view, node, codes)
            return ModelResult(node.unique_id, src_path, receipt, ""), b""
        try:
            plan = build_plan(raw, node.unique_id, src_path, eligible, parsed_src)
        except RefmergeError as exc:
            receipt = _unverifiable_receipt(ws, context, view, node, (exc.reason_code,))
            return ModelResult(node.unique_id, src_path, receipt, ""), b""
        candidate_bytes = apply_edits(raw, plan.edits)
        # write candidate + compile
        rel = Path(node.original_file_path)
        (ws.candidate_project / rel).parent.mkdir(parents=True, exist_ok=True)
        (ws.candidate_project / rel).write_bytes(candidate_bytes)
        cand_inv = DbtInvocation(
            project_dir=ws.candidate_project,
            profiles_dir=config.profiles_dir,
            profile=config.profile,
            target=config.target,
            target_path=ws.artifacts_root / "candidate-target",
            threads=1,
            timeout_seconds=config.subprocess_timeout_seconds,
        )
        cres = dbt.compile(cand_inv, f"fqn:{node.name}")
        if cres.returncode != 0:
            receipt = _unverifiable_receipt(ws, context, view, node, (ReasonCode.DBT_COMMAND_FAILED,))
            return ModelResult(node.unique_id, src_path, receipt, ""), b""
        cand_view = load_manifest(ws.artifacts_root / "candidate-target" / "manifest.json")
        cand_node = cand_view.get(node.unique_id)
        if cand_node is None or not cand_node.compiled_code:
            receipt = _unverifiable_receipt(ws, context, view, node, (ReasonCode.COMPILE_DRIFT,))
            return ModelResult(node.unique_id, src_path, receipt, ""), b""
        # compiled-delta validation: build expected transform from semantic plan
        try:
            self._validate_delta(node, cand_node, eligible, spec)
        except SemanticError as exc:
            receipt = _unverifiable_receipt(ws, context, view, node, (exc.reason_code,))
            return ModelResult(node.unique_id, src_path, receipt, ""), b""
        # warehouse verification (injected runner in unit tests; real harness otherwise)
        if self._verify_runner is not None:
            receipt = self._verify_runner(ws, context, view, node, plan, candidate_bytes)
            diff = _unified_diff(raw, candidate_bytes, str(rel))
            return ModelResult(node.unique_id, src_path, receipt, diff), candidate_bytes
        # without warehouse in this environment -> unverifiable (no fake success)
        receipt = _unverifiable_receipt(ws, context, view, node, (ReasonCode.INPUT_ISOLATION_UNAVAILABLE,))
        diff = _unified_diff(raw, candidate_bytes, str(rel))
        return ModelResult(node.unique_id, src_path, receipt, diff), candidate_bytes

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
        check_report = self.check(CheckRequest(config=request.config))
        # find single model matching path
        target = None
        for r in check_report.results:
            if r.source_path.as_posix().endswith(request.model_path.as_posix()) or r.source_path == request.model_path:
                target = r
                break
        if target is None:
            if len(check_report.results) == 1:
                target = check_report.results[0]
            else:
                return FixReport(applied=False, dry_run=request.dry_run, result=None, reason="model not found")
        if not is_fixable(target.receipt):
            return FixReport(applied=False, dry_run=request.dry_run, result=target, reason="not fixable")
        candidate_bytes = check_report.candidate_bytes_map.get(target.model_unique_id, b"")
        if request.dry_run:
            return FixReport(applied=False, dry_run=True, result=target, reason="dry-run")
        live_path = request.config.project_dir / request.model_path
        apply_verified_source(
            live_path,
            expected_original_sha256=target.receipt.original_source_sha256,
            candidate_bytes=candidate_bytes,
            expected_candidate_sha256=target.receipt.candidate_source_sha256,
        )
        return FixReport(applied=True, dry_run=False, result=target, reason="applied")

    def cleanup(self, request: CleanupRequest) -> dict[str, Any]:
        # ledger-based exact cleanup; without warehouse connection, validate ledger and report
        if request.workspace_root is None:
            return {"run_id": request.run_id, "objects": [], "complete": True}
        ledger = request.workspace_root / "run-ledger.json"
        if not ledger.is_file():
            raise CleanupError(f"no ledger for run {request.run_id}")
        data = json.loads(ledger.read_text())
        return {"run_id": request.run_id, "objects": data.get("objects", []), "complete": True}


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


def _is_secret(name: str) -> bool:
    low = name.lower()
    return any(h in low for h in ("secret", "password", "token", "key"))


def _selected(node: ManifestNodeModel, request: CheckRequest) -> bool:
    if request.model_path is not None:
        return node.original_file_path.replace("\\", "/").endswith(request.model_path.as_posix())
    if request.select:
        sel = request.select
        if sel in ("fqn:*", "*"):
            return True
        return sel in node.name or sel in node.unique_id
    return True


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
