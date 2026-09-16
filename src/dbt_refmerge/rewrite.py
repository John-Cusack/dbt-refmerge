"""Pure rewrite planner + patcher. Never touches the filesystem."""

from __future__ import annotations

import hashlib
from pathlib import Path

from dbt_refmerge.analyze import QualifiedDuplicateGroup
from dbt_refmerge.domain import Identifier, ReasonCode, RewritePlan, SourceSpan, TextEdit
from dbt_refmerge.errors import InternalInvariantError, RewriteError
from dbt_refmerge.source import DownstreamRef, ParsedSourceModel


def validate_edits(edits: tuple[TextEdit, ...], source_size: int) -> None:
    ordered = sorted(edits, key=lambda edit: edit.span)
    previous_end = 0
    for edit in ordered:
        edit.span.validate(source_size)
        if edit.span.start_byte < previous_end:
            raise InternalInvariantError("overlapping source edits")
        previous_end = edit.span.end_byte


def apply_edits(source: bytes, edits: tuple[TextEdit, ...]) -> bytes:
    result = source
    for edit in sorted(edits, key=lambda item: item.span.start_byte, reverse=True):
        result = result[: edit.span.start_byte] + edit.replacement + result[edit.span.end_byte :]
    return result


def _detect_newline_indent(select_list_bytes: bytes) -> tuple[bytes, bytes, bool]:
    """Return (newline_seq, indent, is_multiline). Infer from canonical select list."""
    if b"\n" not in select_list_bytes:
        return b"", b"", False
    # find last newline + indent before end? Use first newline sequence.
    idx = select_list_bytes.find(b"\n")
    if select_list_bytes[idx - 1 : idx] == b"\r":
        nl: bytes = b"\r\n"
    else:
        nl = b"\n"
    # indent = whitespace after first newline (idx points at b"\n"; for CRLF
    # the b"\r" precedes it, so content always starts at idx + 1)
    j = idx + 1
    k = j
    while k < len(select_list_bytes) and select_list_bytes[k : k + 1] in (b" ", b"\t"):
        k += 1
    return nl, select_list_bytes[j:k], True


def build_plan(
    source: bytes,
    model_unique_id: str,
    source_path: Path,
    groups: tuple[QualifiedDuplicateGroup, ...],
    model: ParsedSourceModel,
) -> RewritePlan:
    from dbt_refmerge.source import source_sha256

    original_sha = source_sha256(source)
    cte_by_ident = {c.identifier.identity.value: c for c in model.ctes}
    downstream_by_ident: dict[str, list[DownstreamRef]] = {}
    for ref in model.downstream_refs:
        downstream_by_ident.setdefault(ref.cte_identity.value, []).append(ref)

    edits: list[TextEdit] = []
    canonical_overall: Identifier | None = None
    removed_all: list[Identifier] = []

    def _has_sql_comment(start: int, end: int) -> bool:
        decoded = model.decoded
        return any(
            token.kind == "comment"
            and decoded.char_to_byte[token.start] < end
            and decoded.char_to_byte[token.end] > start
            for token in model.tokens
        )

    def _has_non_ref_jinja(start: int, end: int, allowed_ref_span: SourceSpan | None = None) -> bool:
        for jinja in model.masked.jinja_spans:
            if jinja.span.end_byte <= start or jinja.span.start_byte >= end:
                continue
            if allowed_ref_span is not None and jinja.span == allowed_ref_span:
                continue
            return True
        return False

    for qg in groups:
        members = sorted(qg.group.imports, key=lambda m: m.source_cte.ordinal)
        canonical = members[0].source_cte.identifier
        donors = [m.source_cte.identifier for m in members[1:]]
        if canonical_overall is None:
            canonical_overall = canonical
        # --- projection union ---
        # retain canonical order; append missing outputs in first-seen donor order
        have: dict[str, str] = {}  # output identity -> upstream identity
        for p in members[0].source_cte.projections:
            have[p.output_identifier.identity.value] = p.upstream_identifier.identity.value
        missing: list[tuple[bytes, str]] = []  # (donor fragment bytes, output identity)
        for m in members[1:]:
            for p in m.source_cte.projections:
                out = p.output_identifier.identity.value
                up = p.upstream_identifier.identity.value
                if out not in have:
                    have[out] = up
                    fragment = source[p.span.start_byte : p.span.end_byte]
                    missing.append((fragment, out))
                elif have[out] != up:
                    raise RewriteError(ReasonCode.PROJECTION_COLLISION, f"collision on {out}")
        # --- projection insertion edit ---
        if missing:
            canon_cte = cte_by_ident[canonical.identity.value]
            canonical_tail_end = (
                canon_cte.ref_call.span.start_byte if canon_cte.ref_call is not None else canon_cte.body_span.end_byte
            )
            if _has_sql_comment(canon_cte.select_list_span.end_byte, canonical_tail_end) or _has_non_ref_jinja(
                canon_cte.select_list_span.end_byte,
                canonical_tail_end,
            ):
                raise RewriteError(
                    ReasonCode.COMMENT_RELOCATION_UNSUPPORTED,
                    f"comment or Jinja after canonical projection {canonical.source_text}",
                )
            select_bytes = source[canon_cte.select_list_span.start_byte : canon_cte.select_list_span.end_byte]
            nl, indent, multiline = _detect_newline_indent(select_bytes)
            if not multiline:
                raise RewriteError(
                    ReasonCode.UNSUPPORTED_IMPORT_SHAPE,
                    "single-line select list insertion unsupported",
                )
            # trailing comma style: does select list end with comma?
            trailing_comma = select_bytes.rstrip().endswith(b",")
            insertion = b""
            for frag, _out in missing:
                frag = frag.strip()
                if trailing_comma:
                    insertion += nl + indent + frag + b","
                else:
                    insertion += b"," + nl + indent + frag
            # insert at end of select list span
            edits.append(
                TextEdit(
                    span=SourceSpan(
                        start_byte=canon_cte.select_list_span.end_byte,
                        end_byte=canon_cte.select_list_span.end_byte,
                    ),
                    replacement=insertion,
                    reason_code=ReasonCode.OK,
                )
            )
        # --- donor deletion edits ---
        for donor in donors:
            donor_cte = cte_by_ident[donor.identity.value]
            # deletion span: cte_span extended to consume separator comma ownership
            # Representation: leading trivia | CTE | trailing trivia | optional comma.
            # We delete cte_span; plus separator comma span if present; plus one adjacent newline run
            # without consuming neighbor trivia: extend to include separator span only.
            start = donor_cte.cte_span.start_byte
            end = donor_cte.cte_span.end_byte
            # include separator comma
            if donor_cte.separator_span is not None:
                end = max(end, donor_cte.separator_span.end_byte)
            else:
                # last CTE: need to consume the comma of the previous sibling? Instead previous CTE
                # owns its trailing comma; deleting last CTE leaves dangling comma -> must remove it.
                # Find previous CTE's separator and extend deletion backwards to cover it? That would
                # consume retained CTE trivia. v0.1 approach: delete donor span + preceding comma run.
                # Search backwards for the nearest comma in source between previous CTE end and donor start.
                prev_end = 0
                for c in model.ctes:
                    if c.cte_span.end_byte <= start and c.cte_span.end_byte > prev_end:
                        prev_end = c.cte_span.end_byte
                between = source[prev_end:start]
                comma_idx = between.rfind(b",")
                if comma_idx != -1:
                    start = prev_end + comma_idx
            allowed_ref_span = donor_cte.ref_call.span if donor_cte.ref_call is not None else None
            if _has_sql_comment(start, end) or _has_non_ref_jinja(start, end, allowed_ref_span):
                raise RewriteError(
                    ReasonCode.COMMENT_RELOCATION_UNSUPPORTED,
                    f"comment or Jinja in donor deletion span {donor.source_text}",
                )
            # also strip one trailing newline to avoid blank pile-up (only whitespace)
            while end < len(source) and source[end : end + 1] in (b" ", b"\t"):
                end += 1
            if source[end : end + 2] == b"\r\n":
                end += 2
            elif source[end : end + 1] == b"\n":
                end += 1
            # collapse exactly one extra blank line left by the removed block,
            # but never consume comment- or Jinja-bearing trivia.
            probe = end
            while probe < len(source) and source[probe : probe + 1] in (b" ", b"\t"):
                probe += 1
            if source[probe : probe + 2] == b"\r\n":
                newline_len = 2
            elif source[probe : probe + 1] == b"\n":
                newline_len = 1
            else:
                newline_len = 0
            if newline_len:
                consumed = source[end : probe + newline_len]
                if not any(marker in consumed for marker in (b"--", b"/*", b"{{", b"{%", b"{#")):
                    end = probe + newline_len
            # strip leading blank line similarly if at start
            edits.append(
                TextEdit(
                    span=SourceSpan(start_byte=start, end_byte=end),
                    replacement=b"",
                    reason_code=ReasonCode.OK,
                )
            )
            removed_all.append(donor)
        # --- reference redirection ---
        for donor in donors:
            for ref in downstream_by_ident.get(donor.identity.value, []):
                if ref.has_alias:
                    replacement = canonical.source_text.encode("utf-8")
                else:
                    replacement = canonical.source_text.encode("utf-8") + b" as " + donor.source_text.encode("utf-8")
                edits.append(TextEdit(span=ref.span, replacement=replacement, reason_code=ReasonCode.OK))
        if canonical_overall is None:
            canonical_overall = canonical

    if canonical_overall is None:
        raise RewriteError(ReasonCode.NO_DUPLICATE_IMPORT, "no groups to plan")
    validate_edits(tuple(edits), len(source))
    candidate = apply_edits(source, tuple(edits))
    # reparse guard: transformed group must no longer contain duplicates
    from dbt_refmerge.source import parse_source_model as _parse

    try:
        reparsed = _parse(candidate, fold_unquoted=model.fold_unquoted)
        names = [c.identifier.identity.value for c in reparsed.ctes if c.ref_call is not None]
        for qg in groups:
            remaining = [n for n in names if n in {m.source_cte.identifier.identity.value for m in qg.group.imports}]
            if len(remaining) >= 2:
                raise RewriteError(ReasonCode.INTERNAL_ERROR, "duplicate group survives rewrite")
    except RewriteError:
        raise
    except Exception as exc:
        raise RewriteError(ReasonCode.INTERNAL_ERROR, f"reparse after patch failed: {exc}") from exc
    candidate_sha = hashlib.sha256(candidate).hexdigest()
    return RewritePlan(
        model_unique_id=model_unique_id,
        source_path=source_path,
        original_source_sha256=original_sha,
        canonical_cte=canonical_overall,
        removed_ctes=tuple(removed_all),
        edits=tuple(sorted(edits, key=lambda e: e.span)),
        candidate_source_sha256=candidate_sha,
    )
