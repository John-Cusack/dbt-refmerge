"""Run workspace: stable snapshot, candidate tree, ledger, cleanup."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any

from dbt_refmerge.domain import ReasonCode, RelationIdentity
from dbt_refmerge.errors import CleanupError, RefmergeError

RUNTIME_EXCLUDES = {"target", "logs", "dbt_packages", ".git", "__pycache__", "node_modules"}


def _new_run_id() -> str:
    ts = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    return f"{ts}_{uuid.uuid4().hex[:12]}"


@dataclass
class SnapshotResult:
    root: Path
    file_count: int
    tree_hash: str


@dataclass
class ScratchObject:
    relation: RelationIdentity
    kind: str  # "view"
    state: str = "recorded"  # recorded|created|dropped|missing|cleanup_failed


class RunWorkspace:
    _keep: bool

    def __init__(self, run_id: str, root: Path) -> None:
        self.run_id = run_id
        self.root = root
        self.source_snapshot = root / "source_snapshot"
        self.candidate_project = root / "candidate_project"
        self.harness_project = root / "harness_project"
        self.artifacts_root = root / "artifacts"
        self.ledger_path = root / "run-ledger.json"
        self._objects: list[ScratchObject] = []
        self._dbt_command_log = root / "dbt_logs"

    @classmethod
    def create(cls, keep: bool = False) -> RunWorkspace:
        root = Path(tempfile.mkdtemp(prefix="dbt_refmerge_"))
        ws = cls(run_id=_new_run_id(), root=root)
        ws.source_snapshot.mkdir(parents=True, exist_ok=True)
        ws.candidate_project.mkdir(parents=True, exist_ok=True)
        ws.harness_project.mkdir(parents=True, exist_ok=True)
        ws.artifacts_root.mkdir(parents=True, exist_ok=True)
        ws._keep = keep
        ws._write_ledger()
        return ws

    # -- context manager --
    def __enter__(self) -> RunWorkspace:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if not getattr(self, "_keep", False):
            self.cleanup_files()

    # -- snapshot --
    def snapshot_project(self, project_dir: Path) -> SnapshotResult:
        project_dir = project_dir.resolve()
        entries: list[tuple[str, bytes]] = []
        for dirpath, dirnames, filenames in os.walk(project_dir, followlinks=False):
            # prune runtime excludes + symlinked dirs
            dirnames[:] = [d for d in dirnames if d not in RUNTIME_EXCLUDES]
            # symlink cycle/special handling
            for d in list(dirnames):
                full = Path(dirpath) / d
                if full.is_symlink():
                    target = full.resolve()
                    try:
                        target.relative_to(project_dir)
                    except ValueError as exc:
                        raise RefmergeError(ReasonCode.INTERNAL_ERROR, f"symlink escapes project: {full}") from exc
                    # copy safe contents later; keep dir for walk without following link edits
                    if not target.is_dir():
                        dirnames.remove(d)
                elif not full.is_dir() or full.is_socket() or full.is_fifo():
                    dirnames.remove(d)
            for fn in sorted(filenames):
                full = Path(dirpath) / fn
                if full.is_symlink():
                    target = full.resolve()
                    try:
                        target.relative_to(project_dir)
                    except ValueError as exc:
                        raise RefmergeError(ReasonCode.INTERNAL_ERROR, f"symlink escapes project: {full}") from exc
                    if not target.is_file():
                        raise RefmergeError(ReasonCode.INTERNAL_ERROR, f"unsafe symlink: {full}")
                    data = target.read_bytes()
                elif full.is_socket() or full.is_fifo() or not full.is_file():
                    raise RefmergeError(ReasonCode.INTERNAL_ERROR, f"special file rejected: {full}")
                else:
                    data = full.read_bytes()
                rel = str((full.relative_to(project_dir)).as_posix())
                entries.append((rel, data))
        entries.sort()
        # stability re-stat: re-hash files whose size changed during copy
        for rel, data in entries:
            dest = self.source_snapshot / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
        # verify stability: re-read source mtimes/sizes once more
        for rel, data in entries:
            src = project_dir / rel
            try:
                data2 = src.read_bytes()
            except FileNotFoundError as exc:
                raise RefmergeError(
                    ReasonCode.SOURCE_CHANGED_DURING_SNAPSHOT,
                    f"source changed during snapshot: {rel}",
                ) from exc
            if hashlib.sha256(data2).hexdigest() != hashlib.sha256(data).hexdigest():
                raise RefmergeError(
                    ReasonCode.SOURCE_CHANGED_DURING_SNAPSHOT,
                    f"source changed during snapshot: {rel}",
                )
        tree_hash = hashlib.sha256(b"".join(r.encode() + hashlib.sha256(d).digest() for r, d in entries)).hexdigest()
        # candidate tree = copy of snapshot
        if self.candidate_project.exists():
            shutil.rmtree(self.candidate_project)
        shutil.copytree(self.source_snapshot, self.candidate_project)
        return SnapshotResult(root=self.source_snapshot, file_count=len(entries), tree_hash=tree_hash)

    def package_state_digest(self, root: Path) -> str:
        h = hashlib.sha256()
        candidates = [
            "dbt_project.yml",
            "dbt_project.yaml",
            "packages.yml",
            "package-lock.yml",
            "dependencies.yml",
        ]
        for rel in candidates:
            p = root / rel
            if p.is_file():
                h.update(rel.encode() + p.read_bytes())
        macros = sorted((root / "macros").rglob("*.sql")) if (root / "macros").is_dir() else []
        for p in macros:
            h.update(str(p.relative_to(root)).encode() + p.read_bytes())
        return h.hexdigest()

    # -- ledger --
    def record_object(self, obj: ScratchObject) -> None:
        for i, existing in enumerate(self._objects):
            if existing.relation == obj.relation:
                self._objects[i] = obj
                break
        else:
            self._objects.append(obj)
        self._write_ledger()

    def _write_ledger(self) -> None:
        payload = {
            "run_id": self.run_id,
            "objects": [
                {
                    "database": o.relation.database.value,
                    "schema": o.relation.schema.value,
                    "identifier": o.relation.identifier.value,
                    "kind": o.kind,
                    "state": o.state,
                }
                for o in self._objects
            ],
        }
        self.ledger_path.write_text(json.dumps(payload, indent=2, sort_keys=True))

    def load_ledger(self) -> dict[str, Any]:
        data: dict[str, Any] = json.loads(self.ledger_path.read_text())
        return data

    def cleanup_files(self) -> None:
        try:
            if self.root.exists() and not getattr(self, "_keep", False):
                shutil.rmtree(self.root, ignore_errors=True)
        except Exception as exc:
            raise CleanupError(f"workspace cleanup failed: {exc}") from exc
