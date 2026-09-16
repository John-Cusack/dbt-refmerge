"""Source frontend: decode, Jinja masking, CTE splitting, import parsing. Safety kernel."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

from jinja2 import Environment
from jinja2 import nodes as _jnodes

from dbt_refmerge.adapters import FoldRule, fold_identity
from dbt_refmerge.domain import (
    Identifier,
    IdentifierIdentity,
    JinjaSpan,
    Projection,
    ReasonCode,
    RefCall,
    SourceCTE,
    SourceSpan,
)
from dbt_refmerge.errors import SourceParseError

SENTINEL_PREFIX = "__r"
SENTINEL_SUFFIX = "__"


def _sentinel_name(counter: int) -> str:
    return f"{SENTINEL_PREFIX}{counter}{SENTINEL_SUFFIX}"


def make_identifier(source_text: str, quoted: bool, fold_unquoted: FoldRule = "lower") -> Identifier:
    # strip surrounding quotes for value but keep identity rules
    value = source_text
    if quoted and len(source_text) >= 2 and source_text[0] == '"' and source_text[-1] == '"':
        value = source_text[1:-1].replace('""', '"')
    return Identifier(
        source_text=source_text,
        value=value,
        quoted=quoted,
        identity=IdentifierIdentity(fold_identity(value, quoted, fold_unquoted)),
    )


@dataclass(frozen=True)
class DecodedSource:
    original_bytes: bytes
    text: str
    had_bom: bool
    newline_style: str  # "lf" | "crlf" | "mixed" | "none"
    char_to_byte: tuple[int, ...]  # byte offset of each char index; len = len(text)+1


def decode_source(data: bytes) -> DecodedSource:
    had_bom = data.startswith(b"\xef\xbb\xbf")
    body = data[3:] if had_bom else data
    try:
        text = body.decode("utf-8")  # strict
    except UnicodeDecodeError as exc:
        raise SourceParseError(ReasonCode.INTERNAL_ERROR, f"invalid UTF-8 source: {exc}") from exc
    # newline detection
    has_crlf = "\r\n" in text
    stripped = text.replace("\r\n", "\n")
    has_lf = "\n" in stripped
    has_cr = "\r" in stripped
    if has_crlf and (has_lf or has_cr):
        style = "mixed"
    elif has_crlf:
        style = "crlf"
    elif has_lf or has_cr:
        style = "lf"
    else:
        style = "none"
    # char -> byte table
    table: list[int] = []
    offset = 3 if had_bom else 0
    for ch in text:
        table.append(offset)
        offset += len(ch.encode("utf-8"))
    table.append(offset)
    return DecodedSource(
        original_bytes=data,
        text=text,
        had_bom=had_bom,
        newline_style=style,
        char_to_byte=tuple(table),
    )


def byte_span_of(decoded: DecodedSource, start_char: int, end_char: int) -> SourceSpan:
    return SourceSpan(start_byte=decoded.char_to_byte[start_char], end_byte=decoded.char_to_byte[end_char])


# ---------------------------------------------------------------- Jinja scanner

_JINJA_ENV = Environment(autoescape=False)  # noqa: S701 -- parse-only; never rendered


@dataclass
class MaskedSource:
    decoded: DecodedSource
    masked_bytes: bytes
    masked_text: str
    jinja_spans: list[JinjaSpan]
    ref_calls: dict[str, RefCall]  # sentinel identifier -> call


_SIMPLE_CALL_RE = re.compile(
    r"""\s*(ref|source)\(\s*'([^'\\]+)'\s*(,\s*'([^'\\]+)')?\s*\)\s*""",
    flags=re.DOTALL,
)


def _parse_simple_call(inner: str) -> tuple[str, tuple[str | int, ...], dict[str, str | int]] | None:
    """Fast path for the overwhelmingly common single-quoted literal forms.

    Returns None for anything else (double quotes, kwargs, expressions), in
    which case the caller falls back to the Jinja AST parser. Only accepts
    full-string matches, so behavior agrees with the AST path by construction.
    """
    if '"' in inner or "=" in inner:
        return None
    match = _SIMPLE_CALL_RE.fullmatch(inner)
    if match is None:
        return None
    func, first, _, second = match.groups()
    if func == "source":
        if second is None:
            return None
        return func, (first, second), {}
    return func, (first,) if second is None else (first, second), {}


def _parse_literal_call(inner: str) -> tuple[str, tuple[str | int, ...], dict[str, str | int]] | None:
    """Return (func_name, args, kwargs) for constant-only calls, else None."""
    from typing import Any as _Any

    fast = _parse_simple_call(inner)
    if fast is not None:
        return fast
    try:
        mod = _JINJA_ENV.parse("{{ " + inner + " }}")
    except Exception:
        return None
    try:
        stmts: _Any = mod.body
        out: _Any = stmts[0].nodes[0]
    except Exception:
        return None
    if not isinstance(out, _jnodes.Call):
        return None
    func = out.node
    if not isinstance(func, _jnodes.Name):
        return None
    if func.name not in ("ref", "source"):
        return None
    args: list[str | int] = []
    for a in out.args:
        if isinstance(a, _jnodes.Const):
            if not isinstance(a.value, (str, int)):
                return None
            args.append(a.value)
        else:
            return None
    kwargs: dict[str, str | int] = {}
    for kw in out.kwargs:
        if not isinstance(kw.value, _jnodes.Const) or not isinstance(kw.value.value, (str, int)):
            return None
        kwargs[kw.key] = kw.value.value
    # filters/tests/calls rejected: jinja Call node with func attribute etc already excluded
    return str(func.name), tuple(args), kwargs


def _interpret_ref_call(
    func: str, args: tuple[str | int, ...], kwargs: dict[str, str | int], span: SourceSpan
) -> RefCall | None:
    try:
        if func == "ref":
            package: str | None = None
            name = ""
            version: str | int | None = None
            for k in ("version", "v"):
                if k in kwargs:
                    version = kwargs[k]
            if len(args) == 1 and isinstance(args[0], str):
                name = args[0]
            elif len(args) == 2 and all(isinstance(a, str) for a in args):
                package = str(args[0])
                name = str(args[1])
            elif len(args) == 1 and kwargs:
                if isinstance(args[0], str):
                    name = args[0]
                else:
                    return None
            else:
                return None
            if not name:
                return None
            return RefCall(kind="ref", package=package, name=name, source_name=None, version=version, span=span)
        else:
            if len(args) != 2 or not all(isinstance(a, str) for a in args):
                return None
            return RefCall(
                kind="source",
                package=None,
                name=str(args[1]),
                source_name=str(args[0]),
                version=None,
                span=span,
            )
    except Exception:
        return None


_TOKEN = re.compile(r"\{\{|\}\}|\{%|%\}|\{#|#\}")


def mask_jinja(decoded: DecodedSource) -> MaskedSource:
    text = decoded.text
    n = len(text)
    # byte-level mutable mask over original bytes? operate on chars then map to bytes.
    masked_chars = list(text)
    jinja_spans: list[JinjaSpan] = []
    ref_calls: dict[str, RefCall] = {}
    sentinel_counter = 0
    i = 0
    in_raw = False
    raw_start_char = 0
    while i < n:
        if in_raw:
            # search for {% endraw %}
            m = re.search(r"\{%\s*endraw\s*%}", text[i:])
            if m is None:
                raise SourceParseError(ReasonCode.INTERNAL_ERROR, "unterminated raw block")
            end_char = i + m.end()
            span = byte_span_of(decoded, raw_start_char, end_char)
            jinja_spans.append(
                JinjaSpan(
                    span=span,
                    kind="raw",
                    text=decoded.original_bytes[span.start_byte : span.end_byte],
                )
            )
            for j in range(raw_start_char, end_char):
                if masked_chars[j] not in ("\n", "\r"):
                    masked_chars[j] = " "
            i = end_char
            in_raw = False
            continue
        two = text[i : i + 2]
        if two == "{{":
            end = text.find("}}", i + 2)
            if end == -1:
                raise SourceParseError(ReasonCode.INTERNAL_ERROR, "unterminated Jinja expression")
            end_char = end + 2
            inner = text[i + 2 : end]
            span = byte_span_of(decoded, i, end_char)
            jinja_spans.append(
                JinjaSpan(
                    span=span,
                    kind="expression",
                    text=decoded.original_bytes[span.start_byte : span.end_byte],
                )
            )
            parsed = _parse_literal_call(inner)
            call: RefCall | None = None
            if parsed is not None:
                func, args, kwargs = parsed
                call = _interpret_ref_call(func, args, kwargs, span)
            if call is not None:
                sentinel = _sentinel_name(sentinel_counter)
                sentinel_counter += 1
                width = end_char - i  # chars
                if len(sentinel) > width:
                    # cannot fit sentinel; mask as dynamic (not eligible)
                    for j in range(i, end_char):
                        if masked_chars[j] not in ("\n", "\r"):
                            masked_chars[j] = " "
                else:
                    for k, ch in enumerate(sentinel):
                        masked_chars[i + k] = ch
                    for j in range(i + len(sentinel), end_char):
                        if masked_chars[j] not in ("\n", "\r"):
                            masked_chars[j] = " "
                    ref_calls[sentinel] = call
            else:
                for j in range(i, end_char):
                    if masked_chars[j] not in ("\n", "\r"):
                        masked_chars[j] = " "
                    # mark dynamic jinja: replace with spaces (no sentinel)
            i = end_char
        elif two == "{%":
            end = text.find("%}", i + 2)
            if end == -1:
                raise SourceParseError(ReasonCode.INTERNAL_ERROR, "unterminated Jinja statement")
            end_char = end + 2
            tag_inner = text[i + 2 : end].strip()
            span = byte_span_of(decoded, i, end_char)
            # check raw start
            if re.fullmatch(r"\s*raw\s*", text[i + 2 : end], flags=re.DOTALL):
                in_raw = True
                raw_start_char = i
                i = end_char
                continue
            jinja_spans.append(
                JinjaSpan(
                    span=span,
                    kind="statement",
                    text=decoded.original_bytes[span.start_byte : span.end_byte],
                )
            )
            # strings/escapes inside statements: already masked wholesale
            for j in range(i, end_char):
                if masked_chars[j] not in ("\n", "\r"):
                    masked_chars[j] = " "
            i = end_char
            _ = tag_inner
        elif two == "{#":
            end = text.find("#}", i + 2)
            if end == -1:
                raise SourceParseError(ReasonCode.INTERNAL_ERROR, "unterminated Jinja comment")
            end_char = end + 2
            span = byte_span_of(decoded, i, end_char)
            jinja_spans.append(
                JinjaSpan(
                    span=span,
                    kind="comment",
                    text=decoded.original_bytes[span.start_byte : span.end_byte],
                )
            )
            for j in range(i, end_char):
                if masked_chars[j] not in ("\n", "\r"):
                    masked_chars[j] = " "
            i = end_char
        else:
            i += 1
    masked_text = "".join(masked_chars)
    masked_bytes = masked_text.encode(
        "utf-8"
    )  # masked bytes are positional over chars; multibyte Jinja replaced per char
    # NOTE: masked bytes are positional over chars; source spans remain authoritative in original bytes.
    return MaskedSource(
        decoded=decoded,
        masked_bytes=masked_bytes,
        masked_text=masked_text,
        jinja_spans=jinja_spans,
        ref_calls=ref_calls,
    )


# ---------------------------------------------------------------- Tokenizer (masked SQL, char offsets)


@dataclass(frozen=True)
class Token:
    kind: str  # "word","quoted","string","comment","punct","space"
    text: str
    start: int
    end: int


_TOKEN_RE = re.compile(
    r"(?P<space>\s+)"
    r"|(?P<comment>--[^\n]*|/\*.*?\*/)"
    r"|(?P<string>'(?:[^']|'')*'?|\"(?:[^\"]|\"\")*\"?|`(?:[^`]|``)*`?)"
    r"|(?P<word>__r\d+__|[A-Za-z_\u0080-\uFFFF][A-Za-z0-9_$\u0080-\uFFFF]*)"
    r"|(?P<punct>[(),.;*|=<>!+\-/])",
    flags=re.DOTALL,
)


def is_sentinel_word(text: str) -> bool:
    return bool(re.fullmatch(r"__r\d+__", text))


def tokenize_masked(masked_text: str) -> list[Token]:
    tokens: list[Token] = []
    pos = 0
    for m in _TOKEN_RE.finditer(masked_text):
        if m.start() != pos:
            # single uncovered char (e.g. other punct like :)
            ch = masked_text[pos : m.start()]
            for k, c in enumerate(ch):
                tokens.append(Token(kind="punct", text=c, start=pos + k, end=pos + k + 1))
        kind = m.lastgroup or "punct"
        tokens.append(Token(kind=kind, text=m.group(), start=m.start(), end=m.end()))
        pos = m.end()
    if pos < len(masked_text):
        for k, c in enumerate(masked_text[pos:]):
            tokens.append(Token(kind="punct", text=c, start=pos + k, end=pos + k + 1))
    return tokens


def _significant(tokens: list[Token]) -> list[Token]:
    return [t for t in tokens if t.kind not in ("space", "comment")]


_SENTINEL_GUARD = True


# ---------------------------------------------------------------- CTE splitter


@dataclass(frozen=True)
class DownstreamRef:
    cte_identity: IdentifierIdentity
    span: SourceSpan
    has_alias: bool
    context_cte: str | None  # enclosing CTE name if inside another CTE body


@dataclass
class ParsedSourceModel:
    decoded: DecodedSource
    masked: MaskedSource
    tokens: list[Token]
    ctes: tuple[SourceCTE, ...]
    downstream_refs: tuple[DownstreamRef, ...]
    nested_names: frozenset[str] = frozenset()
    sentinel_to_span: dict[str, SourceSpan] = field(default_factory=dict)


def _char_to_byte(decoded: DecodedSource, ch: int) -> int:
    return decoded.char_to_byte[ch]


def _span_chars(decoded: DecodedSource, s: int, e: int) -> SourceSpan:
    return SourceSpan(start_byte=_char_to_byte(decoded, s), end_byte=_char_to_byte(decoded, e))


def _strip_quotes_ident(tok_text: str) -> tuple[str, bool]:
    if len(tok_text) >= 2 and tok_text[0] == '"' and tok_text[-1] == '"':
        return tok_text, True
    if len(tok_text) >= 2 and tok_text[0] == "`" and tok_text[-1] == "`":
        return tok_text, True
    return tok_text, False


def parse_source_model(data: bytes, *, fold_unquoted: FoldRule = "lower") -> ParsedSourceModel:
    decoded = decode_source(data)
    masked = mask_jinja(decoded)
    tokens = tokenize_masked(masked.masked_text)
    sig = _significant(tokens)
    # O(1) token lookups: the splitter resolves each CTE's parens by position.
    token_index = {(t.start, t.end): i for i, t in enumerate(tokens)}
    sig_index = {(t.start, t.end): i for i, t in enumerate(sig)}
    # find top-level WITH (skip leading config spans: allow word tokens like config? simply find first WITH)
    with_idx: int | None = None
    for idx, tok in enumerate(sig):
        if tok.kind in ("word",) and tok.text.upper() == "WITH":
            # check RECURSIVE
            nxt = sig[idx + 1] if idx + 1 < len(sig) else None
            if nxt is not None and nxt.kind == "word" and nxt.text.upper() == "RECURSIVE":
                raise SourceParseError(ReasonCode.UNSUPPORTED_IMPORT_SHAPE, "WITH RECURSIVE unsupported")
            with_idx = idx
            break
        # if we hit SELECT without WITH -> no CTEs
        if tok.kind == "word" and tok.text.upper() == "SELECT":
            break
    ctes: list[SourceCTE] = []
    downstream: list[DownstreamRef] = []
    if with_idx is None:
        return ParsedSourceModel(decoded=decoded, masked=masked, tokens=tokens, ctes=(), downstream_refs=())
    # iterate CTEs from with_idx+1
    pos = with_idx + 1
    ordinal = 0
    # map char index -> token for paren matching: operate on masked text directly with depth tracking
    # We resolve spans via token char offsets.
    seen_identities: set[str] = set()
    while pos < len(sig):
        tok = sig[pos]
        if tok.kind == "word" and tok.text.upper() in (
            "SELECT",
            "INSERT",
            "UPDATE",
            "DELETE",
            "MERGE",
            "TABLE",
            "VALUES",
        ):
            break  # main query
        # expect CTE name
        if tok.kind == "string" and tok.text.startswith('"'):
            raw_name, quoted = tok.text, True
        elif tok.kind == "word":
            if tok.text.upper() in ("RECURSIVE",):
                raise SourceParseError(ReasonCode.UNSUPPORTED_IMPORT_SHAPE, "WITH RECURSIVE unsupported")
            raw_name, quoted = tok.text, False
        else:
            raise SourceParseError(ReasonCode.UNSUPPORTED_IMPORT_SHAPE, f"unexpected token in CTE list: {tok.text!r}")
        ident = make_identifier(raw_name, quoted, fold_unquoted)
        if ident.identity.value in seen_identities:
            raise SourceParseError(ReasonCode.UNSUPPORTED_IMPORT_SHAPE, "duplicate CTE name")
        seen_identities.add(ident.identity.value)
        pos += 1
        # reject column list
        if pos < len(sig) and sig[pos].kind == "punct" and sig[pos].text == "(":
            # Could be AS ( vs column list. Peek: column list appears BEFORE AS.
            # If next significant after name is '(' and token after that is identifier then check for AS following.
            # Simplest: if sig[pos] is '(' then this is a column list -> reject (AS ( comes after AS keyword)
            raise SourceParseError(ReasonCode.UNSUPPORTED_IMPORT_SHAPE, "CTE column list unsupported")
        # expect AS
        if pos >= len(sig) or not (sig[pos].kind == "word" and sig[pos].text.upper() == "AS"):
            raise SourceParseError(ReasonCode.UNSUPPORTED_IMPORT_SHAPE, "expected AS in CTE")
        pos += 1
        # optional materialization hint NOT MATERIALIZED -> reject
        if pos < len(sig) and sig[pos].kind == "word" and sig[pos].text.upper() == "NOT":
            raise SourceParseError(ReasonCode.UNSUPPORTED_IMPORT_SHAPE, "materialization hint unsupported")
        if pos < len(sig) and sig[pos].kind == "word" and sig[pos].text.upper() == "MATERIALIZED":
            raise SourceParseError(ReasonCode.UNSUPPORTED_IMPORT_SHAPE, "materialization hint unsupported")
        if pos >= len(sig) or not (sig[pos].kind == "punct" and sig[pos].text == "("):
            raise SourceParseError(ReasonCode.UNSUPPORTED_IMPORT_SHAPE, "expected AS ( in CTE")
        open_tok = sig[pos]
        try:
            open_full = token_index[(open_tok.start, open_tok.end)]
        except KeyError:
            raise SourceParseError(ReasonCode.INTERNAL_ERROR, "CTE open paren not found") from None
        d = 0
        close_full = -1
        for fi in range(open_full, len(tokens)):
            t = tokens[fi]
            if t.kind == "string" or t.kind == "comment":
                continue
            if t.kind == "punct" and t.text == "(":
                d += 1
            elif t.kind == "punct" and t.text == ")":
                d -= 1
                if d == 0:
                    close_full = fi
                    break
        if close_full == -1:
            raise SourceParseError(ReasonCode.UNSUPPORTED_IMPORT_SHAPE, "unbalanced CTE parens")
        close_tok = tokens[close_full]
        body_start_char = open_tok.end
        body_end_char = close_tok.start
        # separator: comma right after close (significant)
        # find next significant token after close
        try:
            close_sig = sig_index[(close_tok.start, close_tok.end)]
        except KeyError:
            raise SourceParseError(ReasonCode.INTERNAL_ERROR, "CTE close paren not found") from None
        sep_span: SourceSpan | None = None
        is_last = True
        if close_sig + 1 < len(sig) and sig[close_sig + 1].kind == "punct" and sig[close_sig + 1].text == ",":
            comma = sig[close_sig + 1]
            sep_span = _span_chars(decoded, comma.start, comma.end)
            is_last = False
        # CTE span: from name start to close end
        cte_span = _span_chars(decoded, tok.start, close_tok.end)
        # parse body
        cte = _parse_import_body(
            decoded=decoded,
            masked=masked,
            tokens=tokens,
            ident=ident,
            ordinal=ordinal,
            cte_span=cte_span,
            body_char_range=(body_start_char, body_end_char),
            separator_span=sep_span,
            fold_unquoted=fold_unquoted,
        )
        ctes.append(cte)
        ordinal += 1
        pos = close_sig + (2 if not is_last else 1)
        if is_last:
            break
    model = ParsedSourceModel(decoded=decoded, masked=masked, tokens=tokens, ctes=tuple(ctes), downstream_refs=())
    # downstream refs + nested-CTE shadowing names, each in one pass
    downstream, nested = _find_downstream_refs(model, fold_unquoted)
    model = ParsedSourceModel(
        decoded=decoded,
        masked=masked,
        tokens=tokens,
        ctes=tuple(ctes),
        downstream_refs=tuple(downstream),
        nested_names=nested,
    )
    return model


def _split_depth_zero(body_tokens: list[Token]) -> list[list[Token]]:
    parts: list[list[Token]] = [[]]
    depth = 0
    for t in body_tokens:
        if t.kind not in ("string", "comment"):
            if t.kind == "punct" and t.text == "(":
                depth += 1
            elif t.kind == "punct" and t.text == ")":
                depth -= 1
            elif t.kind == "punct" and t.text == "," and depth == 0:
                parts.append([])
                continue
        parts[-1].append(t)
    return parts


def _parse_import_body(
    *,
    decoded: DecodedSource,
    masked: MaskedSource,
    tokens: list[Token],
    ident: Identifier,
    ordinal: int,
    cte_span: SourceSpan,
    body_char_range: tuple[int, int],
    separator_span: SourceSpan | None,
    fold_unquoted: FoldRule,
) -> SourceCTE:
    body_start, body_end = body_char_range
    body_span = _span_chars(decoded, body_start, body_end)
    # body tokens (significant + positions) within range
    in_range = [t for t in tokens if t.start >= body_start and t.end <= body_end]
    sig = _significant(in_range)
    empty = SourceCTE(
        identifier=ident,
        ordinal=ordinal,
        cte_span=cte_span,
        body_span=body_span,
        select_list_span=body_span,
        separator_span=separator_span,
        ref_call=None,
        projections=(),
        predicate_source_span=None,
    )
    if not sig:
        return empty
    # require SELECT at depth 0 first
    if not (sig[0].kind == "word" and sig[0].text.upper() == "SELECT"):
        return empty  # not an import shape; qualification will reject
    # locate FROM at depth 0
    depth = 0
    from_idx: int | None = None
    where_idx: int | None = None
    for idx, t in enumerate(sig):
        if t.kind not in ("string",):
            if t.kind == "punct" and t.text == "(":
                depth += 1
            elif t.kind == "punct" and t.text == ")":
                depth -= 1
        if depth == 0 and t.kind == "word":
            up = t.text.upper()
            if up == "FROM" and from_idx is None:
                from_idx = idx
            elif up == "WHERE" and from_idx is not None and where_idx is None:
                where_idx = idx
    if from_idx is None:
        return empty
    select_sig = sig[1:from_idx]
    if not select_sig:
        return empty
    # reject DISTINCT at select start
    if select_sig and select_sig[0].kind == "word" and select_sig[0].text.upper() in ("DISTINCT", "ALL"):
        return empty
    # select list span: from first select token start to last select token end
    select_list_span = _span_chars(decoded, select_sig[0].start, select_sig[-1].end)
    # FROM item: tokens between FROM and WHERE/end at depth 0 — must be exactly one sentinel word
    from_end = where_idx if where_idx is not None else len(sig)
    from_sig = sig[from_idx + 1 : from_end]
    # NOTE: any parens in from_sig naturally fail the single-sentinel shape below.
    ref_call: RefCall | None = None
    if len(from_sig) == 1 and from_sig[0].kind == "word" and is_sentinel_word(from_sig[0].text):
        ref_call = masked.ref_calls.get(from_sig[0].text)
    elif len(from_sig) == 2 and from_sig[0].kind == "word" and is_sentinel_word(from_sig[0].text):
        # FROM sentinel alias without AS? e.g. FROM rel alias — but import FROM must be sole relation;
        # allow trailing alias only if... v0.1 import CTEs have no alias on FROM; treat as non-import
        ref_call = None
    else:
        # check for other clauses after from_end (GROUP BY etc.) -> non-import; still record no ref
        pass
    # check trailing clause after WHERE: only WHERE allowed; any GROUP/ORDER/LIMIT/etc -> non-import
    if where_idx is not None:
        # predicate span: from WHERE token start... to end of sig? but must reject other clauses
        rest = sig[where_idx + 1 :]
        # scan for clause keywords that terminate WHERE
        clause_keywords = {
            "GROUP",
            "ORDER",
            "LIMIT",
            "HAVING",
            "WINDOW",
            "QUALIFY",
            "FETCH",
            "UNION",
            "INTERSECT",
            "EXCEPT",
            "OFFSET",
        }
        for t in rest:
            if t.kind == "word" and t.text.upper() in clause_keywords:
                return SourceCTE(
                    identifier=ident,
                    ordinal=ordinal,
                    cte_span=cte_span,
                    body_span=body_span,
                    select_list_span=select_list_span,
                    separator_span=separator_span,
                    ref_call=ref_call,
                    projections=(),
                    predicate_source_span=None,
                )
    # projections: split select_sig on depth-zero commas (need depth relative to select list)
    # rebuild with paren depth
    proj_parts: list[list[Token]] = [[]]
    depth2 = 0
    for t in select_sig:
        if t.kind == "punct" and t.text == "(":
            depth2 += 1
        elif t.kind == "punct" and t.text == ")":
            depth2 -= 1
        if t.kind == "punct" and t.text == "," and depth2 == 0:
            proj_parts.append([])
            continue
        proj_parts[-1].append(t)
    projections: list[Projection] = []
    valid = ref_call is not None
    for part in proj_parts:
        psig = _significant(part)
        if not psig:
            valid = False
            break
        # reject star
        if any(t.kind == "punct" and t.text == "*" for t in psig):
            valid = False
            break
        # reject qualified (dot), function (paren), literals
        if any(t.kind == "punct" and t.text in (".", "(", ")") for t in psig):
            valid = False
            break
        if any(t.kind == "string" and not t.text.startswith('"') for t in psig):
            valid = False
            break
        # shapes: [col] | [col AS alias] | [col alias]
        words = [t for t in psig if t.kind in ("word", "string")]
        if len(words) == 1:
            col_tok = words[0]
            out_tok = words[0]
        elif len(words) == 3 and words[1].kind == "word" and words[1].text.upper() == "AS":
            col_tok, out_tok = words[0], words[2]
        elif len(words) == 2 and words[1].kind in ("word", "string"):
            # implicit alias; ensure no AS missing confusion: first must be plain col
            if words[0].kind != "word" and not (words[0].kind == "string" and words[0].text.startswith('"')):
                valid = False
                break
            col_tok, out_tok = words[0], words[1]
        else:
            valid = False
            break
        # col must be bare identifier (word or quoted), alias likewise
        for candidate in (col_tok, out_tok):
            if candidate.kind == "word":
                if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*|__DBT_REFMERGE_REL_\d+__", candidate.text):
                    valid = False
                    break
            elif candidate.kind == "string" and candidate.text.startswith('"'):
                pass
            else:
                valid = False
                break
        if not valid:
            break
        c_raw, c_quoted = (col_tok.text, col_tok.kind == "string")
        o_raw, o_quoted = (out_tok.text, out_tok.kind == "string")
        span = _span_chars(decoded, psig[0].start, psig[-1].end)
        projections.append(
            Projection(
                upstream_identifier=make_identifier(c_raw, c_quoted, fold_unquoted),
                output_identifier=make_identifier(o_raw, o_quoted, fold_unquoted),
                span=span,
                attached_comment_spans=(),
            )
        )
    # attached comments: any SQL comment tokens inside select list range -> attach to enclosing projection span
    # gather comment tokens in range
    comments = [
        t
        for t in in_range
        if t.kind == "comment" and select_sig and select_sig[0].start <= t.start <= select_sig[-1].end
    ]
    if comments and projections:
        # attach all as belonging to nearest projection; simplest: attach to projection whose span contains them
        new_projs: list[Projection] = []
        for p in projections:
            attached = tuple(
                _span_chars(decoded, c.start, c.end)
                for c in comments
                if p.span.start_byte <= _char_to_byte(decoded, c.start) <= p.span.end_byte
            )
            new_projs.append(
                Projection(
                    upstream_identifier=p.upstream_identifier,
                    output_identifier=p.output_identifier,
                    span=p.span,
                    attached_comment_spans=attached,
                )
            )
        projections = new_projs
        # comments outside every projection span mark relocation unsupported
        for c in comments:
            cb = _char_to_byte(decoded, c.start)
            if not any(p.span.start_byte <= cb <= p.span.end_byte for p in projections):
                valid = False
                break
    predicate_span: SourceSpan | None = None
    if where_idx is not None:
        predicate_span = _span_chars(decoded, sig[where_idx].start, sig[-1].end)
    if not valid:
        # return with ref_call=None to mark unsupported shape (caller distinguishes)
        return SourceCTE(
            identifier=ident,
            ordinal=ordinal,
            cte_span=cte_span,
            body_span=body_span,
            select_list_span=select_list_span,
            separator_span=separator_span,
            ref_call=None,
            projections=tuple(projections),
            predicate_source_span=predicate_span,
        )
    # duplicate output names within one CTE -> keep but qualification rejects
    return SourceCTE(
        identifier=ident,
        ordinal=ordinal,
        cte_span=cte_span,
        body_span=body_span,
        select_list_span=select_list_span,
        separator_span=separator_span,
        ref_call=ref_call,
        projections=tuple(projections),
        predicate_source_span=predicate_span,
    )


def _find_downstream_refs(
    model: ParsedSourceModel, fold_unquoted: FoldRule
) -> tuple[list[DownstreamRef], frozenset[str]]:
    cte_idents = {c.identifier.identity.value: c.identifier for c in model.ctes}
    # body char ranges to exclude (import bodies already parsed) — but downstream refs live outside import SELECT-FROM?
    # Simplest robust approach: scan significant tokens for FROM/JOIN <name> patterns across whole masked text,
    # excluding tokens inside import CTE FROM clause (the sentinel) and inside import select lists.
    # Build exclusion set of char offsets: for each CTE, exclude its select_list+from sentinel? Instead exclude
    # tokens whose char range lies within any CTE body_span AND the CTE is an import (ref_call not None) and
    # token is the sentinel itself. Other tokens inside import bodies (e.g. WHERE cols) are not table refs.
    import_body_ranges = [(c.body_span.start_byte, c.body_span.end_byte) for c in model.ctes if c.ref_call is not None]
    decoded = model.decoded
    sig = _significant(model.tokens)
    refs: list[DownstreamRef] = []
    # nested WITH shadowing: single pass collecting CTE names declared inside a
    # CTE body. A WITH at top level declares a top-level CTE, not a shadow.
    nested_names: set[str] = set()
    body_ranges = [(c.body_span.start_byte, c.body_span.end_byte) for c in model.ctes]
    for idx, t in enumerate(sig):
        if t.kind != "word" or t.text.upper() != "WITH":
            continue
        tb = decoded.char_to_byte[t.start]
        if not any(start <= tb < end for start, end in body_ranges):
            continue
        if idx + 1 < len(sig) and sig[idx + 1].kind in ("word", "string"):
            nm = sig[idx + 1].text
            q = sig[idx + 1].kind == "string"
            nested_names.add(make_identifier(nm, q, fold_unquoted).identity.value)
    for idx, t in enumerate(sig):
        if t.kind == "word" and t.text.upper() in ("FROM", "JOIN"):
            if idx + 1 >= len(sig):
                continue
            name_tok = sig[idx + 1]
            if name_tok.kind not in ("word", "string"):
                continue
            if is_sentinel_word(name_tok.text):
                continue
            q = name_tok.kind == "string"
            ident = make_identifier(name_tok.text, q, fold_unquoted)
            if ident.identity.value not in cte_idents:
                continue
            # skip if this token is inside an import body (e.g. self?) — check byte range
            tb = decoded.char_to_byte[name_tok.start]
            if any(s <= tb < e for s, e in import_body_ranges):
                # Could still be a genuine downstream ref inside another CTE body (non-import CTE).
                # Only skip when inside an import CTE body.
                pass
                # determine enclosing CTE: find cte whose body contains tb and is import
                enclosing_import = any(
                    c.body_span.start_byte <= tb < c.body_span.end_byte and c.ref_call is not None for c in model.ctes
                )
                if enclosing_import:
                    continue
            # alias detection
            has_alias = False
            if idx + 2 < len(sig):
                nxt = sig[idx + 2]
                if nxt.kind == "word" and nxt.text.upper() == "AS":
                    has_alias = True
                elif nxt.kind in ("word", "string") and nxt.text.upper() not in (
                    "LEFT",
                    "RIGHT",
                    "FULL",
                    "INNER",
                    "OUTER",
                    "JOIN",
                    "ON",
                    "USING",
                    "WHERE",
                    "GROUP",
                    "ORDER",
                    "LIMIT",
                    "HAVING",
                    "UNION",
                    "INTERSECT",
                    "EXCEPT",
                    "SELECT",
                    "WITH",
                ):
                    # implicit alias — but must not be a keyword; check next is on new clause
                    has_alias = True
            # enclosing CTE context
            context = None
            for c in model.ctes:
                if c.body_span.start_byte <= tb < c.body_span.end_byte:
                    context = c.identifier.source_text
                    break
            refs.append(
                DownstreamRef(
                    cte_identity=ident.identity,
                    span=_span_chars(decoded, name_tok.start, name_tok.end),
                    has_alias=has_alias,
                    context_cte=context,
                )
            )
    return refs, frozenset(nested_names)


def source_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
