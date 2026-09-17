"""Frozen domain model: identifiers, spans, plans, receipts, reason codes."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Literal


class ReasonCode(str, Enum):
    OK = "OK"
    NO_DUPLICATE_IMPORT = "NO_DUPLICATE_IMPORT"
    UNUSED_IMPORT_COLUMNS = "UNUSED_IMPORT_COLUMNS"
    NEEDS_COMPILED_ANALYSIS = "NEEDS_COMPILED_ANALYSIS"
    UNSUPPORTED_MANIFEST_SCHEMA = "UNSUPPORTED_MANIFEST_SCHEMA"
    UNSUPPORTED_ADAPTER = "UNSUPPORTED_ADAPTER"
    ADAPTER_MISMATCH = "ADAPTER_MISMATCH"
    UNSUPPORTED_MODEL_TYPE = "UNSUPPORTED_MODEL_TYPE"
    UNSUPPORTED_IMPORT_SHAPE = "UNSUPPORTED_IMPORT_SHAPE"
    UNSUPPORTED_COMPARISON_TYPE = "UNSUPPORTED_COMPARISON_TYPE"
    DIFFERENT_PREDICATE = "DIFFERENT_PREDICATE"
    PROJECTION_COLLISION = "PROJECTION_COLLISION"
    SOURCE_MAPPING_AMBIGUOUS = "SOURCE_MAPPING_AMBIGUOUS"
    REFERENCE_BINDING_AMBIGUOUS = "REFERENCE_BINDING_AMBIGUOUS"
    COMMENT_RELOCATION_UNSUPPORTED = "COMMENT_RELOCATION_UNSUPPORTED"
    NONDETERMINISTIC = "NONDETERMINISTIC"
    COMPILE_REQUIRES_INTROSPECTION = "COMPILE_REQUIRES_INTROSPECTION"
    COMPILE_DRIFT = "COMPILE_DRIFT"
    HARNESS_EMBEDDING_UNSAFE = "HARNESS_EMBEDDING_UNSAFE"
    SCRATCH_BOUNDARY_VIOLATION = "SCRATCH_BOUNDARY_VIOLATION"
    INPUT_ISOLATION_UNAVAILABLE = "INPUT_ISOLATION_UNAVAILABLE"
    SCHEMA_MISMATCH = "SCHEMA_MISMATCH"
    BAG_DIFFERENCE = "BAG_DIFFERENCE"
    SOURCE_CHANGED_DURING_SNAPSHOT = "SOURCE_CHANGED_DURING_SNAPSHOT"
    SOURCE_CHANGED_BEFORE_APPLY = "SOURCE_CHANGED_BEFORE_APPLY"
    DBT_COMMAND_FAILED = "DBT_COMMAND_FAILED"
    WAREHOUSE_TIMEOUT = "WAREHOUSE_TIMEOUT"
    RESOURCE_LIMIT = "RESOURCE_LIMIT"
    CLEANUP_FAILED = "CLEANUP_FAILED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class VerificationStatus(str, Enum):
    NOT_RUN = "not_run"
    SNAPSHOT_EQUIVALENT = "snapshot_equivalent"
    DIFFERENT = "different"
    UNVERIFIABLE = "unverifiable"
    ERROR = "error"


@dataclass(frozen=True, order=True)
class IdentifierIdentity:
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

    def validate(self, file_size: int) -> None:
        from dbt_refmerge.errors import InternalInvariantError

        if not (0 <= self.start_byte <= self.end_byte <= file_size):
            raise InternalInvariantError(f"invalid source span {self!r} for size {file_size}")


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
    attached_comment_spans: tuple[SourceSpan, ...] = ()


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
    # Normalized (catalog, db, name) parts of the one relation the compiled import reads.
    relation: tuple[str, ...] = ()


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
