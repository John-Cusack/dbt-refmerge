"""Source frontend: decode, Jinja masking, CTE splitting, import parsing. Safety kernel."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Literal

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
    without_crlf = text.replace("\r\n", "")
    has_lf = "\n" in without_crlf
    has_cr = "\r" in without_crlf
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
    """Return (func_name, args, kwargs) for a ref()/source() call whose arguments are all constants."""
    fast = _parse_simple_call(inner)
    if fast is not None:
        return fast
    try:
        template = _JINJA_ENV.parse("{{ " + inner + " }}")
    except Exception:
        return None
    # Invariant: the scanner ends an expression at its first "}}", so `inner` never contains one and
    # a template that parses is a single {{ }} holding a single expression.
    output = template.body[0]
    assert isinstance(output, _jnodes.Output) and len(template.body) == len(output.nodes) == 1
    call = output.nodes[0]
    if not (isinstance(call, _jnodes.Call) and isinstance(call.node, _jnodes.Name)):
        return None
    if call.node.name not in ("ref", "source") or call.dyn_args is not None or call.dyn_kwargs is not None:
        return None  # *args / **kwargs are runtime values
    args = [a.value for a in call.args if isinstance(a, _jnodes.Const) and isinstance(a.value, (str, int))]
    kwargs = {
        kw.key: kw.value.value
        for kw in call.kwargs
        if isinstance(kw.value, _jnodes.Const) and isinstance(kw.value.value, (str, int))
    }
    if len(args) != len(call.args) or len(kwargs) != len(call.kwargs):
        return None
    return call.node.name, tuple(args), kwargs


def _interpret_ref_call(
    func: str, args: tuple[str | int, ...], kwargs: dict[str, str | int], span: SourceSpan
) -> RefCall | None:
    names = [a for a in args if isinstance(a, str)]
    if len(names) != len(args):
        return None
    if func == "source":
        # dbt's source() takes exactly (source_name, table_name) and no kwargs.
        if len(names) != 2 or kwargs:
            return None
        return RefCall(kind="source", package=None, name=names[1], source_name=names[0], version=None, span=span)
    # ref(name) or ref(package, name) with at most one of version= / v=. dbt reads `version or v` and
    # ignores other kwargs, but a project can override ref(): anything else is not a literal call.
    if len(names) not in (1, 2) or not names[-1] or len(kwargs) > 1 or not set(kwargs) <= {"version", "v"}:
        return None
    return RefCall(
        kind="ref",
        package=names[0] if len(names) == 2 else None,
        name=names[-1],
        source_name=None,
        version=next(iter(kwargs.values()), None),
        span=span,
    )


def _strip_whitespace_control(inner: str) -> str:
    """Drop Jinja's `{{-` / `{{+` / `-}}` markers from an expression's inner text.

    They only strip template whitespace around the tag, which the rewriter never edits: the tag's
    span still covers the markers, and an import's ref sits strictly inside its CTE body.
    """
    if inner[:1] in ("-", "+"):
        inner = inner[1:]
    if inner[-1:] == "-":
        inner = inner[:-1]
    return inner


_TAG_OPEN = re.compile(r"\{[{%#]")
_TAG_CLOSE = {"{{": "}}", "{%": "%}", "{#": "#}"}
_TAG_KIND: dict[str, Literal["expression", "statement", "comment", "raw"]] = {
    "{{": "expression",
    "{%": "statement",
    "{#": "comment",
}
_RAW_OPEN_INNER = re.compile(r"\s*raw\s*")
_RAW_CLOSE = re.compile(r"\{%\s*endraw\s*%}")


def mask_jinja(decoded: DecodedSource) -> MaskedSource:
    """Blank every Jinja tag, keeping newlines, and write a sentinel over each literal ref()/source()."""
    text = decoded.text
    masked_chars = list(text)
    jinja_spans: list[JinjaSpan] = []
    ref_calls: dict[str, RefCall] = {}
    pos = 0
    while (opening := _TAG_OPEN.search(text, pos)) is not None:
        start, tag = opening.start(), opening.group()
        kind = _TAG_KIND[tag]
        close = text.find(_TAG_CLOSE[tag], start + 2)
        if close == -1:
            raise SourceParseError(ReasonCode.INTERNAL_ERROR, f"unterminated Jinja {kind}")
        inner = text[start + 2 : close]
        pos = close + 2
        if tag == "{%" and _RAW_OPEN_INNER.fullmatch(inner):
            raw_close = _RAW_CLOSE.search(text, pos)
            if raw_close is None:
                raise SourceParseError(ReasonCode.INTERNAL_ERROR, "unterminated raw block")
            pos, kind = raw_close.end(), "raw"
        span = byte_span_of(decoded, start, pos)
        jinja_spans.append(
            JinjaSpan(span=span, kind=kind, text=decoded.original_bytes[span.start_byte : span.end_byte])
        )
        for j in range(start, pos):
            if masked_chars[j] not in ("\n", "\r"):
                masked_chars[j] = " "
        parsed = _parse_literal_call(_strip_whitespace_control(inner)) if tag == "{{" else None
        call = _interpret_ref_call(*parsed, span) if parsed is not None else None
        if call is None:
            continue
        sentinel = _sentinel_name(len(ref_calls))
        # The shortest literal call, {{ref('m')}}, is 12 chars; a sentinel is 13 from the 10**7th call.
        if len(sentinel) > pos - start:  # pragma: no cover -- needs 10**7 calls in one file
            continue  # left blank: a dynamic expression, so never an import
        masked_chars[start : start + len(sentinel)] = sentinel
        ref_calls[sentinel] = call
    masked_text = "".join(masked_chars)
    return MaskedSource(
        decoded=decoded,
        # Positional over chars, not bytes: source spans in original bytes stay authoritative.
        masked_bytes=masked_text.encode("utf-8"),
        masked_text=masked_text,
        jinja_spans=jinja_spans,
        ref_calls=ref_calls,
    )


# ---------------------------------------------------------------- Tokenizer (masked SQL, char offsets)


@dataclass(frozen=True)
class Token:
    kind: str  # "space", "comment", "string", "word" or "punct" (any other single char)
    text: str
    start: int
    end: int


_TOKEN_RE = re.compile(
    r"(?P<space>\s+)"
    r"|(?P<comment>--[^\n]*|/\*.*?\*/)"
    r"|(?P<string>'(?:[^']|'')*'?|\"(?:[^\"]|\"\")*\"?|`(?:[^`]|``)*`?)"
    r"|(?P<word>__r\d+__|[A-Za-z_\u0080-\uFFFF][A-Za-z0-9_$\u0080-\uFFFF]*)"
    r"|(?P<punct>.)",
    flags=re.DOTALL,
)


def tokenize_masked(masked_text: str) -> list[Token]:
    # Every char matches some group, so the tokens tile the text.
    return [
        Token(kind=m.lastgroup or "punct", text=m.group(), start=m.start(), end=m.end())
        for m in _TOKEN_RE.finditer(masked_text)
    ]


def _significant(tokens: list[Token]) -> list[Token]:
    return [t for t in tokens if t.kind not in ("space", "comment")]


def _is_word(tokens: list[Token], idx: int, upper: str) -> bool:
    return 0 <= idx < len(tokens) and tokens[idx].kind == "word" and tokens[idx].text.upper() == upper


def _is_punct(tokens: list[Token], idx: int, text: str) -> bool:
    return 0 <= idx < len(tokens) and tokens[idx].kind == "punct" and tokens[idx].text == text


def _identity_for_token(token: Token | None, fold_unquoted: FoldRule) -> str | None:
    """Identity of a bare or double-quoted identifier token; None for anything else."""
    if token is None or not (token.kind == "word" or token.text.startswith('"')):
        return None
    return make_identifier(token.text, token.kind == "string", fold_unquoted).identity.value


# ---------------------------------------------------------------- CTE splitter


@dataclass(frozen=True)
class DownstreamRef:
    cte_identity: IdentifierIdentity
    span: SourceSpan
    has_alias: bool
    context_cte: str | None  # enclosing CTE name if inside another CTE body
    binding_ambiguous: bool = False
    star_expansion: bool = False
    multi_relation: bool = False
    unqualified_identities: frozenset[str] = frozenset()


@dataclass
class ParsedSourceModel:
    decoded: DecodedSource
    masked: MaskedSource
    tokens: list[Token]
    ctes: tuple[SourceCTE, ...]
    downstream_refs: tuple[DownstreamRef, ...]
    nested_names: frozenset[str] = frozenset()
    sentinel_to_span: dict[str, SourceSpan] = field(default_factory=dict)
    fold_unquoted: FoldRule = "lower"


_MAIN_QUERY_KEYWORDS = frozenset({"SELECT", "INSERT", "UPDATE", "DELETE", "MERGE", "TABLE", "VALUES"})


def parse_source_model(data: bytes, *, fold_unquoted: FoldRule = "lower") -> ParsedSourceModel:
    decoded = decode_source(data)
    masked = mask_jinja(decoded)
    tokens = tokenize_masked(masked.masked_text)
    sig = _significant(tokens)
    # The CTE list follows the first WITH, unless a SELECT comes first.
    with_idx = next(
        (idx for idx, tok in enumerate(sig) if tok.kind == "word" and tok.text.upper() in ("WITH", "SELECT")),
        None,
    )
    ctes: list[SourceCTE] = []
    seen_identities: set[str] = set()
    pos = len(sig) if with_idx is None or not _is_word(sig, with_idx, "WITH") else with_idx + 1
    # A main-query keyword (or the end) right after WITH or a comma ends the list: the CTEs in
    # between, if any, come from masked Jinja.
    while pos < len(sig) and not (sig[pos].kind == "word" and sig[pos].text.upper() in _MAIN_QUERY_KEYWORDS):
        name = sig[pos]
        if _is_word(sig, pos, "RECURSIVE"):
            raise SourceParseError(ReasonCode.UNSUPPORTED_IMPORT_SHAPE, "WITH RECURSIVE unsupported")
        if _identity_for_token(name, fold_unquoted) is None:
            raise SourceParseError(ReasonCode.UNSUPPORTED_IMPORT_SHAPE, f"unexpected token in CTE list: {name.text!r}")
        ident = make_identifier(name.text, name.kind == "string", fold_unquoted)
        if ident.identity.value in seen_identities:
            raise SourceParseError(ReasonCode.UNSUPPORTED_IMPORT_SHAPE, "duplicate CTE name")
        seen_identities.add(ident.identity.value)
        if not (_is_word(sig, pos + 1, "AS") and _is_punct(sig, pos + 2, "(")):
            raise SourceParseError(
                ReasonCode.UNSUPPORTED_IMPORT_SHAPE,
                "expected `name AS (`; CTE column lists and materialization hints are unsupported",
            )
        depth = 0
        for close_idx in range(pos + 2, len(sig)):
            paren = sig[close_idx].text if sig[close_idx].kind == "punct" else ""
            depth += (paren == "(") - (paren == ")")
            if depth == 0:
                break
        else:
            raise SourceParseError(ReasonCode.UNSUPPORTED_IMPORT_SHAPE, "unbalanced CTE parens")
        separator = sig[close_idx + 1] if _is_punct(sig, close_idx + 1, ",") else None
        ctes.append(
            _parse_import_body(
                decoded=decoded,
                masked=masked,
                tokens=tokens,
                ident=ident,
                ordinal=len(ctes),
                cte_span=byte_span_of(decoded, name.start, sig[close_idx].end),
                body_char_range=(sig[pos + 2].end, sig[close_idx].start),
                separator_span=byte_span_of(decoded, separator.start, separator.end) if separator else None,
                fold_unquoted=fold_unquoted,
            )
        )
        if separator is None:
            break
        pos = close_idx + 2
    downstream, nested = _find_downstream_refs(decoded, sig, tuple(ctes), fold_unquoted) if ctes else ((), frozenset())
    return ParsedSourceModel(
        decoded=decoded,
        masked=masked,
        tokens=tokens,
        ctes=tuple(ctes),
        downstream_refs=downstream,
        nested_names=nested,
        fold_unquoted=fold_unquoted,
    )


# Clauses that can follow a WHERE predicate in some supported dialect. A predicate has no end
# marker, so these words (any depth) end it. Two-word entries match only as a pair, which keeps
# columns named `start`, `sort` or `cluster` usable in a predicate.
_CLAUSES_AFTER_WHERE: tuple[tuple[str, ...], ...] = (
    ("GROUP",),
    ("HAVING",),
    ("WINDOW",),
    ("QUALIFY",),
    ("ORDER",),
    ("LIMIT",),
    ("OFFSET",),
    ("FETCH",),
    ("UNION",),
    ("INTERSECT",),
    ("EXCEPT",),
    ("MINUS",),
    ("FOR",),
    ("INTO",),
    ("CONNECT", "BY"),
    ("START", "WITH"),
    ("CLUSTER", "BY"),
    ("DISTRIBUTE", "BY"),
    ("SORT", "BY"),
    ("USING", "SAMPLE"),
)

# Column shapes by which tokens are the keyword AS: `col`, `col alias`, `col AS alias`.
_COLUMN_SHAPES = ([False], [False, False], [False, True, False])


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
    """Parse `SELECT columns FROM <literal ref> [WHERE predicate]`; anything else has ref_call None."""
    body_start, body_end = body_char_range
    body_span = byte_span_of(decoded, body_start, body_end)
    in_body = [t for t in tokens if body_start <= t.start and t.end <= body_end]
    sig = _significant(in_body)
    not_an_import = SourceCTE(
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
    # The first FROM at any depth: one inside parens implies a paren in the column list, which no
    # column shape allows, so it never needs to be told apart from the top-level FROM.
    from_idx = next((idx for idx, t in enumerate(sig) if t.kind == "word" and t.text.upper() == "FROM"), 0)
    columns = sig[1:from_idx]
    ref_call = masked.ref_calls.get(sig[from_idx + 1].text) if from_idx and from_idx + 1 < len(sig) else None
    predicate_words = [t.text.upper() if t.kind == "word" else "" for t in sig[from_idx + 3 :]]
    if (
        ref_call is None
        or not _is_word(sig, 0, "SELECT")
        or _is_word(columns, 0, "DISTINCT")
        or _is_word(columns, 0, "ALL")
        or (from_idx + 2 < len(sig) and not _is_word(sig, from_idx + 2, "WHERE"))
        or any(
            tuple(predicate_words[k : k + len(clause)]) == clause
            for k in range(len(predicate_words))
            for clause in _CLAUSES_AFTER_WHERE
        )
    ):
        return not_an_import
    parts: list[list[Token]] = [[]]
    for t in columns:
        if t.kind == "punct" and t.text == ",":
            parts.append([])
        else:
            parts[-1].append(t)
    if len(parts) > 1 and not parts[-1]:
        parts.pop()  # trailing comma before FROM (BigQuery style)
    comments = [
        t for t in in_body if t.kind == "comment" and columns and columns[0].start <= t.start <= columns[-1].end
    ]
    projections: list[Projection] = []
    for part in parts:
        if (
            any(_identity_for_token(t, fold_unquoted) is None for t in part)
            or [_is_word(part, k, "AS") for k in range(len(part))] not in _COLUMN_SHAPES
        ):
            return not_an_import
        column, output = part[0], part[-1]
        projections.append(
            Projection(
                upstream_identifier=make_identifier(column.text, column.kind == "string", fold_unquoted),
                output_identifier=make_identifier(output.text, output.kind == "string", fold_unquoted),
                span=byte_span_of(decoded, part[0].start, part[-1].end),
                attached_comment_spans=tuple(
                    byte_span_of(decoded, c.start, c.end) for c in comments if part[0].start <= c.start <= part[-1].end
                ),
            )
        )
    if sum(len(p.attached_comment_spans) for p in projections) != len(comments):
        return not_an_import  # a comment between columns cannot move with either one
    return SourceCTE(
        identifier=ident,
        ordinal=ordinal,
        cte_span=cte_span,
        body_span=body_span,
        select_list_span=byte_span_of(decoded, columns[0].start, columns[-1].end),
        separator_span=separator_span,
        ref_call=ref_call,
        projections=tuple(projections),
        predicate_source_span=(
            byte_span_of(decoded, sig[from_idx + 2].start, sig[-1].end) if from_idx + 2 < len(sig) else None
        ),
    )


_ALIAS_STOPPERS = frozenset(
    {
        "LEFT",
        "RIGHT",
        "FULL",
        "INNER",
        "OUTER",
        "CROSS",
        "NATURAL",
        "JOIN",
        "ON",
        "USING",
        "WHERE",
        "GROUP",
        "ORDER",
        "LIMIT",
        "HAVING",
        "WINDOW",
        "QUALIFY",
        "FETCH",
        "OFFSET",
        "UNION",
        "INTERSECT",
        "EXCEPT",
        "SELECT",
        "WITH",
    }
)

_FROM_CLAUSE_ENDERS = frozenset(
    {
        "WHERE",
        "GROUP",
        "ORDER",
        "LIMIT",
        "HAVING",
        "WINDOW",
        "QUALIFY",
        "FETCH",
        "OFFSET",
        "UNION",
        "INTERSECT",
        "EXCEPT",
    }
)


def _nested_cte_names(
    decoded: DecodedSource, sig: list[Token], ctes: tuple[SourceCTE, ...], fold_unquoted: FoldRule
) -> frozenset[str]:
    """Identities of CTEs declared anywhere except the top-level WITH list.

    A nested CTE can shadow a top-level one, and neither the rewriter nor the compiled delta gate
    resolves scopes. So every declaration counts, wherever it is (fail closed): ``name AS (``,
    ``name AS [NOT] MATERIALIZED (`` and ``name (columns) AS (``. That covers ``WITH RECURSIVE``,
    every CTE of a nested list, and nested WITHs in the main query. A declaration whose name is
    not visible (masked Jinja) is refused, since it could shadow anything.
    """
    top_level_starts = {cte.cte_span.start_byte for cte in ctes}
    opener: dict[int, int] = {}
    stack: list[int] = []
    for idx in range(len(sig)):
        if _is_punct(sig, idx, "("):
            stack.append(idx)
        elif _is_punct(sig, idx, ")"):
            opener[idx] = stack.pop() if stack else -1
    names: set[str] = set()
    for as_idx in range(len(sig)):
        if not _is_word(sig, as_idx, "AS"):
            continue
        body = as_idx + 1
        while _is_word(sig, body, "NOT") or _is_word(sig, body, "MATERIALIZED"):
            body += 1
        if not _is_punct(sig, body, "("):
            continue
        name_idx = opener.get(as_idx - 1, as_idx) - 1  # step back over a column list
        name = sig[name_idx] if name_idx >= 0 else None
        if name is not None and decoded.char_to_byte[name.start] in top_level_starts:
            continue
        identity = _identity_for_token(name, fold_unquoted)
        if name is None or identity is None or name.text.upper() in ("WITH", "RECURSIVE"):
            raise SourceParseError(ReasonCode.UNSUPPORTED_IMPORT_SHAPE, "nested CTE name is not visible")
        names.add(identity)
    return frozenset(names)


def _comma_relation_indexes(tokens: list[Token]) -> frozenset[int]:
    """Indexes of commas that separate FROM items, tracked per parenthesis depth."""
    indexes: set[int] = set()
    depth = 0
    in_from: dict[int, bool] = {}
    for idx, token in enumerate(tokens):
        keyword = token.text.upper() if token.kind == "word" else ""
        if _is_punct(tokens, idx, "("):
            depth += 1
        elif _is_punct(tokens, idx, ")"):
            in_from.pop(depth, None)
            depth = max(depth - 1, 0)
        elif keyword in ("SELECT", "FROM") or keyword in _FROM_CLAUSE_ENDERS:
            in_from[depth] = keyword == "FROM"
        elif _is_punct(tokens, idx, ",") and in_from.get(depth, False):
            indexes.add(idx)
    return frozenset(indexes)


def _alias_details(tokens: list[Token], name_idx: int, fold_unquoted: FoldRule) -> tuple[bool, str | None]:
    """(has_alias, identity the relation at name_idx is referred to by; None if unreadable)."""
    after = tokens[name_idx + 1 : name_idx + 3]
    if _is_word(after, 0, "AS"):
        return True, _identity_for_token(after[1] if len(after) > 1 else None, fold_unquoted)
    if after and after[0].kind in ("word", "string") and after[0].text.upper() not in _ALIAS_STOPPERS:
        return True, _identity_for_token(after[0], fold_unquoted)
    return False, _identity_for_token(tokens[name_idx], fold_unquoted)


def _qualified_star(tokens: list[Token], alias_identity: str | None, fold_unquoted: FoldRule) -> bool:
    """Whether the scope selects `alias.*`; an alias or qualifier that cannot be read counts."""
    return any(
        _is_punct(tokens, idx + 1, ".")
        and _is_punct(tokens, idx + 2, "*")
        and (alias_identity is None or _identity_for_token(tokens[idx], fold_unquoted) in (alias_identity, None))
        for idx in range(len(tokens))
    )


def _find_downstream_refs(
    decoded: DecodedSource, sig: list[Token], ctes: tuple[SourceCTE, ...], fold_unquoted: FoldRule
) -> tuple[tuple[DownstreamRef, ...], frozenset[str]]:
    """References to top-level CTEs from other CTE bodies and the main query, plus nested CTE names."""
    cte_identities = {cte.identifier.identity.value for cte in ctes}
    # Scopes: each non-import CTE body, then the main query. An import's only relation is its ref, so
    # a FROM in its body (e.g. `extract(year from x)`) names no relation.
    scopes: dict[str | None, list[Token]] = {
        cte.identifier.source_text: [
            t for t in sig if cte.body_span.start_byte <= decoded.char_to_byte[t.start] < cte.body_span.end_byte
        ]
        for cte in ctes
        if cte.ref_call is None
    }
    main_start = ctes[-1].cte_span.end_byte
    scopes[None] = [t for t in sig if decoded.char_to_byte[t.start] >= main_start]
    features: dict[str | None, tuple[bool, bool, frozenset[str], bool, frozenset[int]]] = {}
    for scope, tokens in scopes.items():
        comma_indexes = _comma_relation_indexes(tokens)
        unqualified: set[str] = set()
        for idx, token in enumerate(tokens):
            identity = _identity_for_token(token, fold_unquoted)
            if identity is not None and not (_is_punct(tokens, idx - 1, ".") or _is_punct(tokens, idx + 1, ".")):
                unqualified.add(identity)
        features[scope] = (
            any(_is_word(tokens, idx, "NATURAL") for idx in range(len(tokens))),
            bool(comma_indexes) or any(_is_word(tokens, idx, "JOIN") for idx in range(len(tokens))),
            frozenset(unqualified),
            any(_is_punct(tokens, idx, "*") and _is_word(tokens, idx - 1, "SELECT") for idx in range(len(tokens))),
            comma_indexes,
        )
    refs: list[DownstreamRef] = []
    # FROM/JOIN-bound references first, then comma-bound ones. A comma-bound relation has no FROM or
    # JOIN before it; it is recorded as an ambiguous binding so qualification refuses rather than
    # deleting a donor and leaving a dangling or externally rebound name.
    for comma_bound in (False, True):
        for scope, tokens in scopes.items():
            has_natural, multi_relation, unqualified_identities, bare_star, comma_indexes = features[scope]
            for idx in range(1, len(tokens)):
                bound = (
                    idx - 1 in comma_indexes
                    if comma_bound
                    else _is_word(tokens, idx - 1, "FROM") or _is_word(tokens, idx - 1, "JOIN")
                )
                identity = _identity_for_token(tokens[idx], fold_unquoted)
                if not bound or identity is None or identity not in cte_identities:
                    continue
                has_alias, alias_identity = _alias_details(tokens, idx, fold_unquoted)
                refs.append(
                    DownstreamRef(
                        cte_identity=IdentifierIdentity(identity),
                        span=byte_span_of(decoded, tokens[idx].start, tokens[idx].end),
                        has_alias=has_alias,
                        context_cte=scope,
                        binding_ambiguous=has_natural or comma_bound,
                        star_expansion=bare_star or _qualified_star(tokens, alias_identity, fold_unquoted),
                        multi_relation=multi_relation,
                        unqualified_identities=unqualified_identities,
                    )
                )
    return tuple(refs), _nested_cte_names(decoded, sig, ctes, fold_unquoted)


def source_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def has_unsupported_duplicate_candidates(model: ParsedSourceModel) -> bool:
    """Return whether a duplicate literal-ref group contains an unsupported CTE.

    Unsupported projection syntax deliberately clears ``SourceCTE.ref_call``.
    Without this second look, two obvious literal imports could be reported as
    NO_DUPLICATE_IMPORT, which is a fail-open diagnostic even though no rewrite
    occurs. Dynamic Jinja is not treated as evidence.
    """
    grouped: dict[tuple[object, ...], list[bool]] = {}
    literal_calls = tuple(model.masked.ref_calls.values())
    for cte in model.ctes:
        contained = [
            candidate
            for candidate in literal_calls
            if cte.body_span.start_byte <= candidate.span.start_byte
            and candidate.span.end_byte <= cte.body_span.end_byte
        ]
        # An unsupported CTE counts only when exactly one literal call could be its input.
        call = cte.ref_call or (contained[0] if len(contained) == 1 else None)
        if call is None:
            continue
        key = (call.kind, call.package, call.name, call.source_name, call.version)
        grouped.setdefault(key, []).append(cte.ref_call is not None)
    return any(len(support_flags) >= 2 and not all(support_flags) for support_flags in grouped.values())
