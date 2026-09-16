"""Narrow manifest artifact loader with per-version adapters."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from dbt_refmerge.domain import ReasonCode
from dbt_refmerge.errors import ArtifactError

MAX_ARTIFACT_BYTES = 256 * 1024 * 1024

SUPPORTED_MANIFEST_SCHEMAS = (
    "https://schemas.getdbt.com/dbt/manifest/v12.json",
    "https://schemas.getdbt.com/dbt/manifest/v11.json",
    "https://schemas.getdbt.com/dbt/manifest/v10.json",
)


class ManifestMetadataModel(BaseModel):
    dbt_schema_version: str
    dbt_version: str
    invocation_id: str | None = None
    adapter_type: str | None = None
    project_name: str | None = None

    model_config = ConfigDict(extra="allow")


class DependsOnModel(BaseModel):
    macros: list[str] = Field(default_factory=list)
    nodes: list[str] = Field(default_factory=list)

    model_config = ConfigDict(extra="allow")


class RefArgsModel(BaseModel):
    package: str | None = None
    name: str = ""
    version: str | int | None = None

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
    depends_on: DependsOnModel = Field(default_factory=DependsOnModel)
    refs: list[RefArgsModel] = Field(default_factory=list)
    sources: list[list[str]] = Field(default_factory=list)
    config: dict[str, Any] = Field(default_factory=dict)
    database: str | None = None
    schema_: str | None = Field(default=None, alias="schema")
    alias: str | None = None

    model_config = ConfigDict(extra="allow", populate_by_name=True)


@dataclass(frozen=True)
class ManifestView:
    metadata: ManifestMetadataModel
    nodes: dict[str, ManifestNodeModel]
    sources: dict[str, dict[str, Any]]
    path: Path

    def models(self) -> list[ManifestNodeModel]:
        return [n for n in self.nodes.values() if n.resource_type == "model"]

    def get(self, unique_id: str) -> ManifestNodeModel | None:
        return self.nodes.get(unique_id)


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in pairs:
        if k in out:
            raise ArtifactError(ReasonCode.INTERNAL_ERROR, f"duplicate JSON key: {k}")
        out[k] = v
    return out


def load_manifest(path: Path | str) -> ManifestView:
    """Load and validate a manifest. Every unusable input raises ArtifactError, never a bare exception."""
    p = Path(path)
    try:
        size = p.stat().st_size
        if size > MAX_ARTIFACT_BYTES:
            raise ArtifactError(ReasonCode.INTERNAL_ERROR, f"manifest too large: {size} bytes")
        data = p.read_bytes()
    except OSError as exc:
        raise ArtifactError(ReasonCode.INTERNAL_ERROR, f"cannot read manifest: {exc}") from exc
    try:
        text = data.decode("utf-8")  # strict; json.loads(bytes) would also accept UTF-16/32
    except UnicodeDecodeError as exc:
        raise ArtifactError(ReasonCode.INTERNAL_ERROR, f"manifest is not valid UTF-8: {exc}") from exc
    # ValueError covers JSONDecodeError and over-long integer literals; RecursionError covers deep nesting.
    try:
        raw: Any = json.loads(text, object_pairs_hook=_no_duplicate_keys)
    except (ValueError, RecursionError, ArtifactError) as exc:
        raise ArtifactError(ReasonCode.INTERNAL_ERROR, f"invalid manifest JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ArtifactError(ReasonCode.INTERNAL_ERROR, "manifest root must be an object")
    metadata_raw = raw.get("metadata")
    if not isinstance(metadata_raw, dict):
        raise ArtifactError(ReasonCode.INTERNAL_ERROR, "manifest metadata missing")
    try:
        metadata = ManifestMetadataModel(**metadata_raw)
    except Exception as exc:
        raise ArtifactError(ReasonCode.INTERNAL_ERROR, f"invalid manifest metadata: {exc}") from exc
    if metadata.dbt_schema_version not in SUPPORTED_MANIFEST_SCHEMAS:
        raise ArtifactError(
            ReasonCode.UNSUPPORTED_MANIFEST_SCHEMA,
            f"unsupported manifest schema: {metadata.dbt_schema_version}",
        )
    # A malformed entry is refused, never skipped: dropping one of two same-named nodes (or sources)
    # would turn an ambiguous ref()/source() into a unique one.
    nodes_raw = raw.get("nodes", {})
    if not isinstance(nodes_raw, dict):
        raise ArtifactError(ReasonCode.INTERNAL_ERROR, "manifest nodes must be an object")
    nodes: dict[str, ManifestNodeModel] = {}
    for uid, node_raw in nodes_raw.items():
        if not isinstance(node_raw, dict):
            raise ArtifactError(ReasonCode.INTERNAL_ERROR, f"invalid node {uid}: not an object")
        try:
            node = ManifestNodeModel(**node_raw)
        except Exception as exc:
            raise ArtifactError(ReasonCode.INTERNAL_ERROR, f"invalid node {uid}: {exc}") from exc
        # depends_on and resolution use the key, callers use unique_id: they must name the same node.
        if node.unique_id != uid:
            raise ArtifactError(ReasonCode.INTERNAL_ERROR, f"node key {uid} does not match its unique_id")
        # path traversal guard: reject absolute or parent-escaping paths
        ofp = node.original_file_path.replace("\\", "/")
        if ofp.startswith("/") or ".." in ofp.split("/") or PureWindowsPath(node.original_file_path).drive:
            raise ArtifactError(ReasonCode.INTERNAL_ERROR, f"unsafe original_file_path: {uid}")
        nodes[uid] = node
    sources = raw.get("sources", {})
    if not isinstance(sources, dict):
        raise ArtifactError(ReasonCode.INTERNAL_ERROR, "manifest sources must be an object")
    for uid, source in sources.items():
        if not isinstance(source, dict):
            raise ArtifactError(ReasonCode.INTERNAL_ERROR, f"invalid source {uid}: not an object")
    return ManifestView(metadata=metadata, nodes=nodes, sources=sources, path=p)


def resolve_literal_ref(
    view: ManifestView,
    owner: ManifestNodeModel,
    kind: str,
    package: str | None,
    name: str,
    source_name: str | None = None,
) -> str:
    """Resolve a literal ref()/source() to exactly one unique_id or raise.

    When the owner carries refs/sources metadata, it must confirm the literal call: metadata that
    contradicts every candidate means the manifest and the source disagree, which is refused rather
    than resolved from the unfiltered candidates.
    """
    candidates: list[str] = []
    if kind == "ref":
        for uid in owner.depends_on.nodes:
            node = view.nodes.get(uid)
            if node is None:
                continue
            if node.resource_type not in ("model", "seed", "snapshot"):
                continue
            if node.name != name:
                continue
            if package is not None and node.package_name != package:
                continue
            candidates.append(uid)
        if owner.refs:
            candidates = [
                uid
                for uid in candidates
                if any(
                    (r.package or package or view.nodes[uid].package_name) == view.nodes[uid].package_name
                    and r.name == name
                    for r in owner.refs
                )
            ]
    else:
        sname = source_name or ""
        for uid, src in view.sources.items():
            if not isinstance(src, dict):
                # No name to compare: it may be another candidate, so the mapping is unknowable.
                raise ArtifactError(ReasonCode.INTERNAL_ERROR, f"invalid source {uid}: not an object")
            if src.get("source_name") == sname and src.get("name") == name:
                candidates.append(uid)
        if owner.sources and [sname, name] not in owner.sources:
            candidates = []
    unique = sorted(set(candidates))
    if len(unique) != 1:
        raise ArtifactError(
            ReasonCode.SOURCE_MAPPING_AMBIGUOUS,
            f"ambiguous {kind} resolution for {name}: {len(unique)} candidates",
        )
    return unique[0]
