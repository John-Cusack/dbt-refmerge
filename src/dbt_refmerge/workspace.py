"""Run workspace: stable snapshot, candidate tree, ledger, cleanup."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import sys
import tempfile
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dbt_refmerge.domain import ReasonCode, RelationIdentity
from dbt_refmerge.errors import CleanupError, RefmergeError

# dbt's default target-path and log-path, VCS metadata and JS tooling: skipped only at the project root, so
# a models/logs/ directory is still copied. dbt_packages is copied: the snapshot must compile with them.
ROOT_EXCLUDES = frozenset({"target", "logs", ".git", "node_modules"})
# Bytecode caches are never dbt inputs, at any depth.
EXCLUDES_AT_ANY_DEPTH = frozenset({"__pycache__"})

_IO_REPARSE_TAG_MOUNT_POINT = 0xA0000003  # stat.IO_REPARSE_TAG_MOUNT_POINT, defined only on Windows


def _new_run_id() -> str:
    ts = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    return f"{ts}_{uuid.uuid4().hex[:12]}"


def _is_linked_dir(path: Path) -> bool:
    """A symlink or a Windows directory junction; ``os.walk`` descends into junctions even without followlinks."""
    st = os.lstat(path)
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_reparse_tag", 0) == _IO_REPARSE_TAG_MOUNT_POINT)


def _read_project_file(project_dir: Path, path: Path) -> bytes:
    """Read a regular file, or a symlink to a regular file inside the project; refuse anything else."""
    source = path
    if path.is_symlink():
        try:
            source = Path(os.path.realpath(path, strict=True))
        except OSError as exc:  # dangling, or a loop
            raise RefmergeError(ReasonCode.INTERNAL_ERROR, f"unsafe symlink: {path}") from exc
        if not source.is_relative_to(project_dir):
            raise RefmergeError(ReasonCode.INTERNAL_ERROR, f"symlink escapes project: {path}")
    if not source.is_file():
        raise RefmergeError(ReasonCode.INTERNAL_ERROR, f"special file rejected: {path}")
    return source.read_bytes()


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
    def __init__(self, run_id: str, root: Path, *, keep: bool = False) -> None:
        self.run_id = run_id
        self.root = root
        self.source_snapshot = root / "source_snapshot"
        self.candidate_project = root / "candidate_project"
        self.harness_project = root / "harness_project"
        self.artifacts_root = root / "artifacts"
        self.ledger_path = root / "run-ledger.json"
        self._objects: list[ScratchObject] = []
        self._dbt_command_log = root / "dbt_logs"
        self._keep = keep

    @classmethod
    def create(cls, keep: bool = False) -> RunWorkspace:
        root = Path(tempfile.mkdtemp(prefix="dbt_refmerge_"))
        ws = cls(run_id=_new_run_id(), root=root, keep=keep)
        ws.source_snapshot.mkdir(parents=True, exist_ok=True)
        ws.candidate_project.mkdir(parents=True, exist_ok=True)
        ws.harness_project.mkdir(parents=True, exist_ok=True)
        ws.artifacts_root.mkdir(parents=True, exist_ok=True)
        ws._write_ledger()
        return ws

    # -- snapshot --
    def snapshot_project(self, project_dir: Path) -> SnapshotResult:
        """Copy the project into ``source_snapshot`` and ``candidate_project``, then re-read it to prove stability.

        Linked directories (symlinks, junctions) are refused rather than followed: skipping them silently drops
        models and following them needs containment and cycle checks. File symlinks are read through when their
        target is a regular file inside the project.
        """
        project_dir = project_dir.resolve()
        self.source_snapshot.mkdir(parents=True, exist_ok=True)
        digests: list[tuple[str, bytes]] = []
        for dirpath, dirnames, filenames in os.walk(project_dir, followlinks=False):
            here = Path(dirpath)
            excluded = (ROOT_EXCLUDES | EXCLUDES_AT_ANY_DEPTH) if here == project_dir else EXCLUDES_AT_ANY_DEPTH
            dirnames[:] = [d for d in dirnames if d not in excluded]
            for d in dirnames:
                if _is_linked_dir(here / d):
                    raise RefmergeError(
                        ReasonCode.INTERNAL_ERROR, f"linked directory in project is not supported: {here / d}"
                    )
            for fn in filenames:
                full = here / fn
                data = _read_project_file(project_dir, full)
                rel = full.relative_to(project_dir).as_posix()
                dest = self.source_snapshot / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(data)
                digests.append((rel, hashlib.sha256(data).digest()))
        digests.sort()
        # Stability: every source file must still hold the bytes that were copied.
        for rel, digest in digests:
            try:
                current = (project_dir / rel).read_bytes()
            except FileNotFoundError as exc:
                raise RefmergeError(
                    ReasonCode.SOURCE_CHANGED_DURING_SNAPSHOT,
                    f"source changed during snapshot: {rel}",
                ) from exc
            if hashlib.sha256(current).digest() != digest:
                raise RefmergeError(
                    ReasonCode.SOURCE_CHANGED_DURING_SNAPSHOT,
                    f"source changed during snapshot: {rel}",
                )
        tree_hash = hashlib.sha256(b"".join(rel.encode() + digest for rel, digest in digests)).hexdigest()
        shutil.copytree(self.source_snapshot, self.candidate_project, dirs_exist_ok=True)
        return SnapshotResult(root=self.source_snapshot, file_count=len(digests), tree_hash=tree_hash)

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
        """Remove the workspace unless it is kept; raise ``CleanupError`` if anything under it survives."""
        if self._keep or not self.root.exists():
            return
        failures: list[str] = []

        def record(function: Callable[..., object], path: str, exc: BaseException) -> None:
            failures.append(f"{path}: {exc}")

        if sys.version_info >= (3, 12):  # pragma: >=3.12 cover
            shutil.rmtree(self.root, onexc=record)
        else:  # pragma: <3.12 cover
            shutil.rmtree(self.root, onerror=lambda function, path, exc_info: record(function, path, exc_info[1]))
        if failures:
            raise CleanupError(f"could not remove {len(failures)} path(s) under {self.root}; first: {failures[0]}")
