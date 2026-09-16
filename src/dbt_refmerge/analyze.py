"""Analysis orchestration: grouping + qualification (no edits)."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

from dbt_refmerge.domain import ReasonCode
from dbt_refmerge.semantics import MatchedCTE

if TYPE_CHECKING:
    from dbt_refmerge.source import DownstreamRef


class FindingStatus(str, Enum):
    MERGE_ELIGIBLE = "merge_eligible"
    NOT_ELIGIBLE = "not_eligible"
    NEEDS_COMPILED_ANALYSIS = "needs_compiled_analysis"


@dataclass(frozen=True)
class Finding:
    model_unique_id: str
    source_path: Path
    upstream_unique_id: str
    cte_names: tuple[str, ...]
    status: FindingStatus
    reason_codes: tuple[ReasonCode, ...]


@dataclass(frozen=True)
class DuplicateGroup:
    model_unique_id: str
    upstream_unique_id: str
    imports: tuple[MatchedCTE, ...]


@dataclass(frozen=True)
class QualifiedDuplicateGroup:
    group: DuplicateGroup
    status: FindingStatus
    reason_codes: tuple[ReasonCode, ...]


def group_imports(imports: list[MatchedCTE], model_unique_id: str) -> tuple[DuplicateGroup, ...]:
    grouped: dict[tuple[str, str], list[MatchedCTE]] = defaultdict(list)
    for item in imports:
        grouped[(model_unique_id, item.upstream_unique_id)].append(item)
    result: list[DuplicateGroup] = []
    for key in sorted(grouped):
        members = sorted(grouped[key], key=lambda item: item.source_cte.ordinal)
        if len(members) >= 2:
            result.append(DuplicateGroup(model_unique_id=key[0], upstream_unique_id=key[1], imports=tuple(members)))
    return tuple(result)


def qualify_group(
    group: DuplicateGroup,
    *,
    downstream_refs: tuple[DownstreamRef, ...] = (),
    whole_model_ok: bool = True,
    nested_names: frozenset[str] = frozenset(),
    star_blocked: bool = False,
    comment_blocked: bool = False,
) -> QualifiedDuplicateGroup:
    reasons: list[ReasonCode] = []
    fps = {m.semantic.predicate_fingerprint for m in group.imports}
    if len(fps) != 1:
        reasons.append(ReasonCode.DIFFERENT_PREDICATE)
    # projection union collision: one output identity -> different upstream identities
    mapping: dict[str, str] = {}
    for m in group.imports:
        for p in m.semantic.projections:
            out = p.output_identity.value
            up = p.upstream_identity.value
            if out in mapping and mapping[out] != up:
                if ReasonCode.PROJECTION_COLLISION not in reasons:
                    reasons.append(ReasonCode.PROJECTION_COLLISION)
            else:
                mapping[out] = up
    # duplicate output within single import
    for m in group.imports:
        outs = [p.output_identity.value for p in m.semantic.projections]
        if len(set(outs)) != len(outs):
            if ReasonCode.PROJECTION_COLLISION not in reasons:
                reasons.append(ReasonCode.PROJECTION_COLLISION)
    # nested shadow
    for m in group.imports:
        if m.semantic.cte_identity.value in nested_names:
            reasons.append(ReasonCode.REFERENCE_BINDING_AMBIGUOUS)
            break
    member_by_identity = {m.semantic.cte_identity.value: m for m in group.imports}
    final_outputs = {
        projection.output_identity.value for member in group.imports for projection in member.semantic.projections
    }
    for ref in downstream_refs:
        member = member_by_identity.get(ref.cte_identity.value)
        if member is None:
            continue
        if ref.binding_ambiguous and ReasonCode.REFERENCE_BINDING_AMBIGUOUS not in reasons:
            reasons.append(ReasonCode.REFERENCE_BINDING_AMBIGUOUS)
        member_outputs = {projection.output_identity.value for projection in member.semantic.projections}
        gained_outputs = final_outputs - member_outputs
        if (
            ref.multi_relation
            and gained_outputs.intersection(ref.unqualified_identities)
            and ReasonCode.REFERENCE_BINDING_AMBIGUOUS not in reasons
        ):
            reasons.append(ReasonCode.REFERENCE_BINDING_AMBIGUOUS)
        if ref.star_expansion and ReasonCode.UNSUPPORTED_IMPORT_SHAPE not in reasons:
            reasons.append(ReasonCode.UNSUPPORTED_IMPORT_SHAPE)
    # donor binding: every donor must have resolvable downstream refs or be referenced;
    # unreferenced donors are still mergeable (dead CTE removal is safe if truly unreferenced?)
    # v0.1: require binding check only for referenced donors; unreferenced donor removal allowed.
    if not whole_model_ok:
        reasons.append(ReasonCode.NONDETERMINISTIC)
    if star_blocked and ReasonCode.UNSUPPORTED_IMPORT_SHAPE not in reasons:
        reasons.append(ReasonCode.UNSUPPORTED_IMPORT_SHAPE)
    if comment_blocked:
        reasons.append(ReasonCode.COMMENT_RELOCATION_UNSUPPORTED)
    if reasons:
        # dedupe preserving order
        seen: list[ReasonCode] = []
        for r in reasons:
            if r not in seen:
                seen.append(r)
        return QualifiedDuplicateGroup(group=group, status=FindingStatus.NOT_ELIGIBLE, reason_codes=tuple(seen))
    return QualifiedDuplicateGroup(group=group, status=FindingStatus.MERGE_ELIGIBLE, reason_codes=(ReasonCode.OK,))
