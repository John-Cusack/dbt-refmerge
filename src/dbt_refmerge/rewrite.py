"""Pure rewrite planner + patcher. Never touches the filesystem."""

from __future__ import annotations

import hashlib
from pathlib import Path

from dbt_refmerge.analyze import FindingStatus, QualifiedDuplicateGroup
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
    all_donor_identities = {
        member.source_cte.identifier.identity.value
        for qualified in groups
        for member in sorted(qualified.group.imports, key=lambda item: item.source_cte.ordinal)[1:]
    }
    terminal_donor_start = len(model.ctes)
    while (
        terminal_donor_start > 0
        and model.ctes[terminal_donor_start - 1].identifier.identity.value in all_donor_identities
    ):
        terminal_donor_start -= 1
    terminal_donors = model.ctes[terminal_donor_start:]
    terminal_donor_identities = {cte.identifier.identity.value for cte in terminal_donors}
    terminal_deletion_start: int | None = None
    terminal_deletion_owner: str | None = None
    terminal_ref_spans: tuple[SourceSpan, ...] = ()
    if terminal_donors:
        # A group's canonical CTE precedes its donors, so a CTE is always retained before the terminal run,
        # and parse_source_model gives every CTE but the last a separator. These guards rely on the source
        # parser, so they stay typed refusals rather than asserts (which vanish under -O and abort the run).
        predecessor_separator = model.ctes[terminal_donor_start - 1].separator_span if terminal_donor_start else None
        if predecessor_separator is None:  # pragma: no cover
            raise RewriteError(ReasonCode.UNSUPPORTED_IMPORT_SHAPE, "terminal donor separator is unavailable")
        terminal_deletion_start = predecessor_separator.start_byte
        terminal_deletion_owner = terminal_donors[0].identifier.identity.value
        terminal_ref_spans = tuple(cte.ref_call.span for cte in terminal_donors if cte.ref_call is not None)

    def _has_sql_comment(start: int, end: int) -> bool:
        decoded = model.decoded
        return any(
            token.kind == "comment"
            and decoded.char_to_byte[token.start] < end
            and decoded.char_to_byte[token.end] > start
            for token in model.tokens
        )

    def _has_non_ref_jinja(
        start: int,
        end: int,
        allowed_ref_spans: tuple[SourceSpan, ...] = (),
    ) -> bool:
        for jinja in model.masked.jinja_spans:
            if jinja.span.end_byte <= start or jinja.span.start_byte >= end:
                continue
            if jinja.span in allowed_ref_spans:
                continue
            return True
        return False

    for qg in groups:
        if qg.status is not FindingStatus.MERGE_ELIGIBLE:
            raise RewriteError(qg.reason_codes[0], f"group is not merge eligible: {qg.status.value}")
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
            removed_all.append(donor)
            if donor.identity.value in terminal_donor_identities and donor.identity.value != terminal_deletion_owner:
                continue
            # deletion span: cte_span extended to consume separator comma ownership
            # Representation: leading trivia | CTE | trailing trivia | optional comma.
            # We delete cte_span; plus separator comma span if present; plus one adjacent newline run
            # without consuming neighbor trivia: extend to include separator span only.
            start = donor_cte.cte_span.start_byte
            end = donor_cte.cte_span.end_byte
            allowed_ref_spans: tuple[SourceSpan, ...] = (
                (donor_cte.ref_call.span,) if donor_cte.ref_call is not None else ()
            )
            # include separator comma
            if donor.identity.value == terminal_deletion_owner:
                assert terminal_deletion_start is not None  # set together with terminal_deletion_owner
                start = terminal_deletion_start
                end = terminal_donors[-1].cte_span.end_byte
                allowed_ref_spans = terminal_ref_spans
                terminal_tail_end = min(
                    (
                        model.decoded.char_to_byte[token.start]
                        for token in model.tokens
                        if token.kind not in ("space", "comment") and model.decoded.char_to_byte[token.start] >= end
                    ),
                    default=len(source),
                )
                if _has_sql_comment(end, terminal_tail_end) or _has_non_ref_jinja(end, terminal_tail_end):
                    raise RewriteError(
                        ReasonCode.COMMENT_RELOCATION_UNSUPPORTED,
                        f"comment or Jinja after terminal donor {donor.source_text}",
                    )
            else:
                # A donor outside the terminal run has a retained CTE after it, so it is not last.
                if donor_cte.separator_span is None:  # pragma: no cover
                    raise RewriteError(ReasonCode.UNSUPPORTED_IMPORT_SHAPE, "donor separator is unavailable")
                end = max(end, donor_cte.separator_span.end_byte)
            if _has_sql_comment(start, end) or _has_non_ref_jinja(start, end, allowed_ref_spans):
                raise RewriteError(
                    ReasonCode.COMMENT_RELOCATION_UNSUPPORTED,
                    f"comment or Jinja in donor deletion span {donor.source_text}",
                )
            if donor.identity.value != terminal_deletion_owner:
                # Also strip one trailing newline to avoid blank pile-up (only whitespace).
                while end < len(source) and source[end : end + 1] in (b" ", b"\t"):
                    end += 1
                if source[end : end + 2] == b"\r\n":
                    end += 2
                elif source[end : end + 1] == b"\n":
                    end += 1
                # Collapse exactly one extra blank line left by the removed block. The line holds only
                # spaces and tabs, so it cannot carry a comment or Jinja.
                probe = end
                while probe < len(source) and source[probe : probe + 1] in (b" ", b"\t"):
                    probe += 1
                if source[probe : probe + 2] == b"\r\n":
                    end = probe + 2
                elif source[probe : probe + 1] == b"\n":
                    end = probe + 1
            # strip leading blank line similarly if at start
            edits.append(
                TextEdit(
                    span=SourceSpan(start_byte=start, end_byte=end),
                    replacement=b"",
                    reason_code=ReasonCode.OK,
                )
            )
        # --- reference redirection ---
        for donor in donors:
            for ref in downstream_by_ident.get(donor.identity.value, []):
                if ref.has_alias:
                    replacement = canonical.source_text.encode("utf-8")
                else:
                    replacement = canonical.source_text.encode("utf-8") + b" as " + donor.source_text.encode("utf-8")
                edits.append(TextEdit(span=ref.span, replacement=replacement, reason_code=ReasonCode.OK))

    if canonical_overall is None:
        raise RewriteError(ReasonCode.NO_DUPLICATE_IMPORT, "no groups to plan")
    validate_edits(tuple(edits), len(source))
    candidate = apply_edits(source, tuple(edits))
    # reparse guard: transformed group must no longer contain duplicates. Defense in depth: the edits only
    # delete whole donor CTEs with their separators, append projections and rename references, so the
    # candidate re-parses and no donor survives; no input is known to reach either refusal.
    from dbt_refmerge.source import parse_source_model as _parse

    try:
        reparsed = _parse(candidate, fold_unquoted=model.fold_unquoted)
        names = [c.identifier.identity.value for c in reparsed.ctes if c.ref_call is not None]
        for qg in groups:
            remaining = [n for n in names if n in {m.source_cte.identifier.identity.value for m in qg.group.imports}]
            if len(remaining) >= 2:
                raise RewriteError(ReasonCode.INTERNAL_ERROR, "duplicate group survives rewrite")  # pragma: no cover
    except RewriteError:  # pragma: no cover
        raise
    except Exception as exc:  # pragma: no cover
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
