"""Compiled semantic analysis: dialect parsing, allowlist, fingerprints, volatility, delta gate."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

import sqlglot
import sqlglot.expressions as exp

from dbt_refmerge.artifacts import ManifestNodeModel, ManifestView, resolve_literal_ref
from dbt_refmerge.domain import (
    IdentifierIdentity,
    ReasonCode,
    SemanticImport,
    SemanticProjection,
    SourceCTE,
)
from dbt_refmerge.errors import SemanticError

COMPARATOR_VERSION = "1"
SEMANTIC_FINGERPRINT_VERSION = "2"

# Function names are lowercase; they match a call's own name and sqlglot's normalized name for it,
# compared without underscores (sqlglot parses gen_random_uuid() as Uuid and string_agg as GroupConcat).
VOLATILE_FUNCTIONS = frozenset(
    {
        "random",
        "rand",
        "setseed",
        "gen_random_uuid",
        "uuid",
        "uuid_generate_v1",
        "uuid_generate_v4",
        "uuid_generate_v1mc",
        "uuid_generate_v5",
        "nextval",
        "currval",
        "lastval",
        "clock_timestamp",
        "statement_timestamp",
        "transaction_timestamp",
        "timeofday",
        "pg_sleep",
        "txid_current",
    }
)

ORDER_SENSITIVE_FUNCTIONS = frozenset(
    {
        "row_number",
        "rank",
        "dense_rank",
        "lag",
        "lead",
        "first_value",
        "last_value",
        "nth_value",
        "array_agg",
        "string_agg",
        "group_concat",
        "json_agg",
        "json_array_agg",
        "jsonb_agg",
    }
)
_REFUSED_FLAT = frozenset(v.replace("_", "") for v in VOLATILE_FUNCTIONS | ORDER_SENSITIVE_FUNCTIONS)

# Allowlist of sqlglot function classes (exact class names, not SQL spellings) whose result depends only
# on their arguments, or is STABLE: fixed for one statement, which is all the single-statement
# baseline/candidate comparison needs. Judged for PostgreSQL, the only adapter that verifies.
# Calls sqlglot does not recognize (exp.Anonymous: UDFs, schema-qualified calls) are never allowlisted.
DETERMINISTIC_FUNCTIONS = frozenset(
    {
        # math: abs, ceil/ceiling, floor, round, trunc, power/^, sqrt, exp, ln, log
        "Abs",
        "Ceil",
        "Floor",
        "Round",
        "Trunc",
        "Pow",
        "Sqrt",
        "Exp",
        "Ln",
        "Log",
        # null handling, conditionals (CASE WHEN parses as Case holding If nodes), boolean connectives
        "Coalesce",
        "Nullif",
        "Greatest",
        "Least",
        "Case",
        "If",
        "And",
        "Or",
        # strings: lower, upper, trim/btrim/ltrim/rtrim, substring/substr, replace, concat, concat_ws,
        # length/char_length, strpos/position, split_part
        "Lower",
        "Upper",
        "Trim",
        "Substring",
        "Replace",
        "Concat",
        "ConcatWs",
        "Length",
        "StrPosition",
        "SplitPart",
        # conversion and dates: cast/::, to_char, to_number, to_timestamp(text, fmt), to_timestamp(epoch),
        # to_date, date_part/extract, date_trunc (TimestampTrunc; DateTrunc in some dialects)
        "Cast",
        "TimeToStr",
        "ToNumber",
        "StrToTime",
        "UnixToTime",
        "StrToDate",
        "Extract",
        "TimestampTrunc",
        "DateTrunc",
        # order-insensitive aggregates (window frames are checked separately)
        "Count",
        "Sum",
        "Min",
        "Max",
        "Avg",
        # STABLE within a statement: now()/current_timestamp, current_date, current_time, localtimestamp, localtime
        "CurrentTimestamp",
        "CurrentDate",
        "CurrentTime",
        "Localtimestamp",
        "Localtime",
    }
)


@dataclass(frozen=True)
class ParsedModel:
    sql: str
    dialect: str
    tree: Any
    ctes: dict[str, Any]  # normalized identity -> CTE expression


@dataclass(frozen=True)
class MatchedCTE:
    source_cte: SourceCTE
    upstream_unique_id: str
    semantic: SemanticImport


@dataclass(frozen=True)
class Qualification:
    ok: bool
    reason_codes: tuple[ReasonCode, ...]
    unexpected_nodes: tuple[str, ...] = ()


def _normalize_ident(name: str, quoted: bool, fold: str = "lower") -> str:
    if quoted or fold == "none":
        return name
    return name.lower() if fold == "lower" else name.upper()


def _fold_for_dialect(dialect: str) -> str:
    from dbt_refmerge.adapters import spec_for_dialect

    return spec_for_dialect(dialect).fold_unquoted


def _alias_name_and_quoted(alias_expr: Any) -> tuple[str, bool]:
    """Extract the identifier carried by sqlglot's TableAlias wrapper."""
    identifier = alias_expr.args.get("this") if hasattr(alias_expr, "args") else None
    if isinstance(identifier, exp.Identifier):
        return identifier.name, bool(identifier.args.get("quoted"))
    # sqlglot's parser always wraps a CTE alias in an Identifier; this only guards an API change.
    name = alias_expr.name if hasattr(alias_expr, "name") else str(alias_expr)  # pragma: no cover
    return str(name), False  # pragma: no cover


def _top_level_with(tree: Any) -> Any:
    # sqlglot>=~27 stores top-level CTEs under "with_"; older versions (26.x) use "with".
    top_with = tree.args.get("with_")
    if top_with is None:
        top_with = tree.args.get("with")
    return top_with


def _cte_identity(cte: Any, fold: str) -> str:
    """Normalized name of a CTE; empty when it has none (a zero-length quoted name such as ``""``)."""
    alias_expr = cte.args.get("alias")
    if alias_expr is None:  # pragma: no cover -- sqlglot's parser gives every CTE a TableAlias
        return ""
    alias_name, quoted = _alias_name_and_quoted(alias_expr)
    return _normalize_ident(alias_name, quoted, fold)


def parse_model(sql: str, dialect: str = "postgres") -> ParsedModel:
    stripped = sql.strip()
    if not stripped:
        raise SemanticError(ReasonCode.INTERNAL_ERROR, "empty compiled SQL")
    # reject raw-block terminator smuggling and multi-statement tricks early
    if "%}" in stripped or "{{" in stripped or "{%" in stripped:
        raise SemanticError(ReasonCode.HARNESS_EMBEDDING_UNSAFE, "unresolved Jinja in compiled SQL")
    try:
        trees = sqlglot.parse(stripped, read=dialect)
    except Exception as exc:
        raise SemanticError(ReasonCode.INTERNAL_ERROR, f"compiled parse failed: {exc}") from exc
    trees = [t for t in trees if t is not None]
    if len(trees) != 1:
        raise SemanticError(ReasonCode.INTERNAL_ERROR, "compiled SQL must be one statement")
    tree = trees[0]
    if isinstance(tree, exp.Semicolon):
        raise SemanticError(ReasonCode.INTERNAL_ERROR, "unexpected semicolon node")
    if not isinstance(tree, (exp.Select, exp.Union, exp.Query)):
        raise SemanticError(ReasonCode.INTERNAL_ERROR, f"non-embeddable compiled root: {type(tree).__name__}")
    # sqlglot's parser splits statements before building nodes, so none can sit inside the root.
    if tree.find(exp.Semicolon) is not None:  # pragma: no cover
        raise SemanticError(ReasonCode.INTERNAL_ERROR, "terminal second statement")
    ctes: dict[str, Any] = {}
    top_with = _top_level_with(tree)
    scope_ctes: list[Any] = []
    if top_with is not None:
        scope_ctes = top_with.args.get("expressions", []) or []
    for cte in scope_ctes:
        ident = _cte_identity(cte, _fold_for_dialect(dialect))
        if ident:
            ctes[ident] = cte
    return ParsedModel(sql=stripped, dialect=dialect, tree=tree, ctes=ctes)


# Strict import allowlist: only these classes may appear inside an import CTE body.
_IMPORT_ALLOWED = frozenset(
    {
        "Select",
        "Column",
        "Identifier",
        "Alias",
        "From",
        "Table",
        "Db",
        "TableAlias",
        "Where",
        "EQ",
        "NEQ",
        "GT",
        "GTE",
        "LT",
        "LTE",
        "And",
        "Or",
        "Not",
        "Paren",
        "Boolean",
        "Literal",
        "Null",
        "Is",
        "In",
        "Between",
        "Cast",
    }
)


def _iter_nodes(node: Any) -> Any:
    """Yield expression nodes across sqlglot walk API variants (bare or tuple)."""
    for item in node.walk(bfs=False):
        yield item[0] if isinstance(item, tuple) else item


def qualify_import_cte(cte_expr: Any) -> Qualification:
    unexpected: list[str] = []
    inner = cte_expr.args.get("this")
    if not isinstance(inner, exp.Select):
        return Qualification(
            ok=False,
            reason_codes=(ReasonCode.UNSUPPORTED_IMPORT_SHAPE,),
            unexpected_nodes=(type(inner).__name__,),
        )
    # reject joins, group, having, windows, distinct, order, limit, etc.
    if inner.args.get("joins"):
        return Qualification(
            ok=False,
            reason_codes=(ReasonCode.UNSUPPORTED_IMPORT_SHAPE,),
            unexpected_nodes=("Join",),
        )
    for key in (
        "group",
        "having",
        "windows",
        "distribute",
        "sort",
        "order",
        "limit",
        "offset",
        "sample",
        "qualify",
        "cluster",
    ):
        if inner.args.get(key):
            return Qualification(
                ok=False,
                reason_codes=(ReasonCode.UNSUPPORTED_IMPORT_SHAPE,),
                unexpected_nodes=(key,),
            )
    if inner.args.get("distinct"):
        return Qualification(
            ok=False,
            reason_codes=(ReasonCode.UNSUPPORTED_IMPORT_SHAPE,),
            unexpected_nodes=("Distinct",),
        )
    if inner.find(exp.Star) is not None:
        return Qualification(
            ok=False,
            reason_codes=(ReasonCode.UNSUPPORTED_IMPORT_SHAPE,),
            unexpected_nodes=("Star",),
        )
    unsupported_func = next(
        (
            node
            for node in _iter_nodes(inner)
            if isinstance(node, exp.Func) and type(node).__name__ not in _IMPORT_ALLOWED
        ),
        None,
    )
    if unsupported_func is not None:
        # Any actual function call inside an import is rejected. sqlglot also
        # models allowed boolean operators such as And/Or as Func subclasses,
        # so the class allowlist must be consulted before refusing.
        return Qualification(
            ok=False,
            reason_codes=(ReasonCode.UNSUPPORTED_IMPORT_SHAPE,),
            unexpected_nodes=(type(unsupported_func).__name__,),
        )
    for n in _iter_nodes(inner):
        cname = type(n).__name__
        if cname not in _IMPORT_ALLOWED:
            unexpected.append(cname)
    if unexpected:
        return Qualification(
            ok=False,
            reason_codes=(ReasonCode.UNSUPPORTED_IMPORT_SHAPE,),
            unexpected_nodes=tuple(sorted(set(unexpected))),
        )
    # FROM must be exactly one table
    from_expr = inner.args.get("from_") or inner.args.get("from")
    if from_expr is None:
        return Qualification(
            ok=False,
            reason_codes=(ReasonCode.UNSUPPORTED_IMPORT_SHAPE,),
            unexpected_nodes=("MissingFrom",),
        )
    # A second relation needs a Join, Subquery, Lateral or similar node, all refused above.
    tables = list(inner.find_all(exp.Table))
    if len(tables) != 1:  # pragma: no cover
        return Qualification(
            ok=False,
            reason_codes=(ReasonCode.UNSUPPORTED_IMPORT_SHAPE,),
            unexpected_nodes=("TableCount",),
        )
    return Qualification(ok=True, reason_codes=(ReasonCode.OK,))


def _canonical(node: Any, fold: str = "lower") -> Any:
    """Versioned canonical serializer: tuple form without positions or comments (sqlglot keeps both off args)."""
    if isinstance(node, list):
        return tuple(_canonical(v, fold) for v in node)
    if isinstance(node, exp.Expression):
        name = type(node).__name__
        args: dict[str, Any] = {}
        for k, v in node.args.items():
            # An unset arg and an explicit None/[] generate identical SQL; the parser fills some
            # (e.g. TableAlias.columns) that synthesized nodes omit, so neither may affect the hash.
            if v is None or v == []:
                continue
            args[k] = _canonical(v, fold)
        if isinstance(node, exp.Identifier):
            quoted = bool(node.args.get("quoted"))
            nm = node.name
            args = {"this": _normalize_ident(nm, quoted, fold), "quoted": quoted}
        if isinstance(node, exp.Literal):
            args = {"this": node.this, "is_string": node.is_string}
        return (name, tuple(sorted(args.items())))
    return node


def canonical_fingerprint(
    node: Any,
    version: str = SEMANTIC_FINGERPRINT_VERSION,
    *,
    fold: str = "lower",
) -> str:
    canon = _canonical(node, fold)
    payload = json.dumps({"v": version, "ast": repr(canon)}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def predicate_fingerprint(cte_expr: Any, *, fold: str = "lower") -> str | None:
    inner = cte_expr.args.get("this")
    if not isinstance(inner, exp.Select):
        return None
    where = inner.args.get("where")
    if where is None:
        return None
    return canonical_fingerprint(where, fold=fold)


def semantic_fingerprint(tree: Any, *, fold: str = "lower") -> str:
    return canonical_fingerprint(tree, fold=fold)


def match_source_ctes(
    source_ctes: tuple[SourceCTE, ...],
    parsed: ParsedModel,
    manifest: ManifestView,
    owner: ManifestNodeModel,
) -> tuple[MatchedCTE, ...]:
    matched: list[MatchedCTE] = []
    fold = _fold_for_dialect(parsed.dialect)
    for scte in source_ctes:
        if scte.ref_call is None:
            continue
        ident = scte.identifier.identity.value
        compiled = parsed.ctes.get(ident)
        if compiled is None:
            raise SemanticError(ReasonCode.SOURCE_MAPPING_AMBIGUOUS, f"no compiled CTE for {ident}")
        qual = qualify_import_cte(compiled)
        if not qual.ok:
            raise SemanticError(
                qual.reason_codes[0],
                f"import shape unsupported for {ident}: {qual.unexpected_nodes}",
            )
        # resolve upstream
        rc = scte.ref_call
        upstream = resolve_literal_ref(manifest, owner, rc.kind, rc.package, rc.name, rc.source_name)
        # qualify_import_cte accepted it: a Select reading exactly one relation
        inner = compiled.args.get("this")
        assert isinstance(inner, exp.Select)
        tables = list(inner.find_all(exp.Table))
        assert len(tables) == 1
        relation = tuple(
            _normalize_ident(part.name, bool(part.args.get("quoted")), fold)
            for part in (tables[0].args.get(key) for key in ("catalog", "db", "this"))
            if isinstance(part, exp.Identifier)
        )
        # compare projections
        compiled_projs = inner.args.get("expressions", []) or []
        if len(compiled_projs) != len(scte.projections):
            raise SemanticError(ReasonCode.SOURCE_MAPPING_AMBIGUOUS, f"projection count drift for {ident}")
        sem_projs: list[SemanticProjection] = []
        for idx, (cproj, sproj) in enumerate(zip(compiled_projs, scte.projections, strict=True)):
            # unwrap alias
            out_name = ""
            out_quoted = False
            up_name = ""
            up_quoted = False
            if isinstance(cproj, exp.Alias):
                alias = cproj.alias
                out_name = alias
                out_quoted = (
                    bool(cproj.args.get("alias", {}).args.get("quoted"))
                    if hasattr(cproj.args.get("alias"), "args")
                    else False
                )
                col = cproj.args.get("this")
                if isinstance(col, exp.Column):
                    parts = col.parts
                    if len(parts) != 1:
                        raise SemanticError(ReasonCode.SOURCE_MAPPING_AMBIGUOUS, "qualified projection")
                    up_name = parts[0].name
                    up_quoted = bool(parts[0].args.get("quoted"))
                else:
                    raise SemanticError(ReasonCode.SOURCE_MAPPING_AMBIGUOUS, "non-column projection")
            elif isinstance(cproj, exp.Column):
                parts = cproj.parts
                if len(parts) != 1:
                    raise SemanticError(ReasonCode.SOURCE_MAPPING_AMBIGUOUS, "qualified projection")
                up_name = parts[0].name
                up_quoted = bool(parts[0].args.get("quoted"))
                out_name, out_quoted = up_name, up_quoted
            else:
                raise SemanticError(ReasonCode.SOURCE_MAPPING_AMBIGUOUS, "unexpected projection node")
            if _normalize_ident(up_name, up_quoted, fold) != sproj.upstream_identifier.identity.value:
                raise SemanticError(ReasonCode.SOURCE_MAPPING_AMBIGUOUS, f"upstream drift for {ident}")
            if _normalize_ident(out_name, out_quoted, fold) != sproj.output_identifier.identity.value:
                raise SemanticError(ReasonCode.SOURCE_MAPPING_AMBIGUOUS, f"output drift for {ident}")
            sem_projs.append(
                SemanticProjection(
                    upstream_identity=IdentifierIdentity(_normalize_ident(up_name, up_quoted, fold)),
                    output_identity=IdentifierIdentity(_normalize_ident(out_name, out_quoted, fold)),
                    ast_path=("ctes", ident, idx),
                )
            )
        # predicate presence cross-check
        inner_where = inner.args.get("where") is not None
        src_where = scte.predicate_source_span is not None
        if inner_where != src_where:
            raise SemanticError(ReasonCode.SOURCE_MAPPING_AMBIGUOUS, f"predicate presence drift for {ident}")
        fp = predicate_fingerprint(compiled, fold=fold)
        matched.append(
            MatchedCTE(
                source_cte=scte,
                upstream_unique_id=upstream,
                semantic=SemanticImport(
                    cte_identity=scte.identifier.identity,
                    upstream_unique_id=upstream,
                    projections=tuple(sem_projs),
                    predicate_fingerprint=fp,
                    ast_path=("ctes", ident),
                    relation=relation,
                ),
            )
        )
    # require unique mapping
    seen: set[str] = set()
    for m in matched:
        key = m.semantic.cte_identity.value
        if key in seen:
            raise SemanticError(ReasonCode.SOURCE_MAPPING_AMBIGUOUS, f"duplicate semantic match {key}")
        seen.add(key)
    return tuple(matched)


@dataclass(frozen=True)
class VolatilityResult:
    ok: bool
    reason_codes: tuple[ReasonCode, ...]
    unknown_functions: tuple[str, ...]


def analyze_volatility(parsed: ParsedModel) -> VolatilityResult:
    """Refuse a model whose rows one statement comparing baseline and candidate cannot pin down."""
    unknown: set[str] = set()
    for node in _iter_nodes(parsed.tree):
        if not isinstance(node, exp.Func):
            continue
        if isinstance(node, exp.Anonymous):
            name = node.name.lower()
            spellings = {name}
            allowlisted = False
        else:
            name = node.sql_name().lower()
            spellings = {name, type(node).__name__.lower()}
            allowlisted = type(node).__name__ in DETERMINISTIC_FUNCTIONS
        if {spelling.replace("_", "") for spelling in spellings} & _REFUSED_FLAT:
            return VolatilityResult(ok=False, reason_codes=(ReasonCode.NONDETERMINISTIC,), unknown_functions=(name,))
        if not allowlisted:
            unknown.add(name)
    # Row sampling, and any query level that picks a subset of rows without its own ORDER BY.
    if parsed.tree.find(exp.TableSample) is not None:
        return VolatilityResult(ok=False, reason_codes=(ReasonCode.NONDETERMINISTIC,), unknown_functions=("sample",))
    # A ROWS frame counts physical rows, so ties make even an allowlisted aggregate order-dependent.
    # RANGE and GROUPS frames always include every peer row.
    for spec in parsed.tree.find_all(exp.WindowSpec):
        if str(spec.args.get("kind") or "").lower() not in ("range", "groups"):
            return VolatilityResult(
                ok=False, reason_codes=(ReasonCode.NONDETERMINISTIC,), unknown_functions=("window_frame",)
            )
    fold = _fold_for_dialect(parsed.dialect)
    for query in _iter_nodes(parsed.tree):
        if not isinstance(query, exp.Query):
            continue
        distinct = query.args.get("distinct")
        limits_rows = query.args.get("limit") is not None or query.args.get("offset") is not None
        distinct_on = isinstance(distinct, exp.Distinct) and distinct.args.get("on") is not None
        if not (limits_rows or distinct_on):
            continue
        order = query.args.get("order")
        prefix = "unordered" if order is None else "partially_ordered"
        if order is not None and _orders_every_output(query, order, fold):
            continue
        unordered = f"{prefix}_limit" if limits_rows else f"{prefix}_distinct_on"
        return VolatilityResult(
            ok=False,
            reason_codes=(ReasonCode.NONDETERMINISTIC,),
            unknown_functions=(unordered,),
        )
    if unknown:
        return VolatilityResult(
            ok=False,
            reason_codes=(ReasonCode.NONDETERMINISTIC,),
            unknown_functions=tuple(sorted(set(unknown))),
        )
    return VolatilityResult(ok=True, reason_codes=(ReasonCode.OK,), unknown_functions=())


def _orders_every_output(query: Any, order: Any, fold: str) -> bool:
    """True when the ORDER BY covers every output column of ``query``.

    Rows that tie on such an ORDER BY are identical in every output column, so LIMIT / OFFSET / DISTINCT ON
    return the same multiset whichever tied row the planner picks. A key covers a column by ordinal, by
    output name, or by being the same expression. ``*`` cannot be checked and never counts as covered.
    """
    outputs = list(query.selects)
    if not outputs or any(o.is_star for o in outputs):
        return False
    ordinals: set[int] = set()
    names: set[str] = set()
    expressions: list[Any] = []
    for ordered in order.expressions:
        key = ordered.this
        if isinstance(key, exp.Literal) and not key.is_string and key.this.isdigit():
            ordinals.add(int(key.this))
        else:
            if isinstance(key, exp.Column) and not key.table:
                names.add(_normalize_ident(key.name, bool(key.this.args.get("quoted")), fold))
            expressions.append(_canonical(key, fold))
    for position, output in enumerate(outputs, start=1):
        name = ""
        if isinstance(output, (exp.Alias, exp.Column)):
            identifier = output.args["alias"] if isinstance(output, exp.Alias) else output.this
            name = _normalize_ident(identifier.name, bool(identifier.args.get("quoted")), fold)
        if position in ordinals or name in names or _canonical(output.unalias(), fold) in expressions:
            continue
        return False
    return True


def validate_compiled_delta(baseline: ParsedModel, candidate: ParsedModel, expected_fingerprint: str) -> None:
    actual = semantic_fingerprint(candidate.tree, fold=_fold_for_dialect(candidate.dialect))
    if actual != expected_fingerprint:
        raise SemanticError(ReasonCode.COMPILE_DRIFT, "candidate compiled AST differs from expected transform")


def build_expected_transform(
    baseline: ParsedModel,
    canonical_ident: str,
    donor_idents: tuple[str, ...],
    added_projections_sql: dict[str, list[str]],
) -> Any:
    """Deep-copy baseline AST, apply merge, return expected tree. Pure sqlglot transform."""
    import copy

    fold = _fold_for_dialect(baseline.dialect)
    donors = set(donor_idents)
    tree = copy.deepcopy(baseline.tree)
    top_with = _top_level_with(tree)
    if top_with is None:
        raise SemanticError(ReasonCode.COMPILE_DRIFT, "baseline has no WITH")
    ctes = list(top_with.args.get("expressions", []) or [])
    canonical = next((c for c in ctes if _cte_identity(c, fold) == canonical_ident), None)
    if canonical is None:
        raise SemanticError(ReasonCode.COMPILE_DRIFT, "canonical CTE missing in baseline")
    # add missing projections to canonical
    inner = canonical.args.get("this")
    if not isinstance(inner, exp.Select):
        raise SemanticError(ReasonCode.COMPILE_DRIFT, "canonical not a select")
    existing = inner.args.get("expressions", []) or []
    additions = added_projections_sql.get(canonical_ident, [])
    for proj_sql in additions:
        try:
            node = sqlglot.parse_one(f"SELECT {proj_sql}", read=baseline.dialect).args["expressions"][0]
        except Exception as exc:
            raise SemanticError(ReasonCode.COMPILE_DRIFT, f"bad projection {proj_sql}: {exc}") from exc
        existing.append(node)
    inner.set("expressions", existing)
    # remove donors
    remaining = [c for c in ctes if _cte_identity(c, fold) not in donors]
    top_with.set("expressions", remaining)
    # redirect table refs bound to donors: rename table to canonical + alias donor
    canon_alias_expr = canonical.args.get("alias")
    canon_name, canon_quoted = _alias_name_and_quoted(canon_alias_expr)
    for scope_table in tree.find_all(exp.Table):
        # CTE references are unqualified; "db"."sch"."stg" is a physical relation even if a donor is named stg.
        if scope_table.args.get("db") is not None or scope_table.args.get("catalog") is not None:
            continue
        tname = scope_table.name
        table_identifier = scope_table.args.get("this")
        table_quoted = (
            bool(table_identifier.args.get("quoted")) if isinstance(table_identifier, exp.Identifier) else False
        )
        if _normalize_ident(tname, table_quoted, fold) in donors:
            # preserve alias behavior: if table already aliased, keep alias; else add alias = donor
            existing_alias = scope_table.args.get("alias")
            donor_raw = tname
            donor_quoted = table_quoted
            # rename to canonical spelling (use canonical alias text from baseline)
            scope_table.set("this", exp.to_identifier(canon_name, quoted=canon_quoted))
            if existing_alias is None:
                scope_table.set("alias", exp.TableAlias(this=exp.to_identifier(donor_raw, quoted=donor_quoted)))
    return tree
