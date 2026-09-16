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
SEMANTIC_FINGERPRINT_VERSION = "1"

VOLATILE_FUNCTIONS = frozenset(
    {
        "random",
        "rand",
        "setseed",
        "gen_random_uuid",
        "uuid_generate_v1",
        "uuid_generate_v4",
        "uuid_generate_v1mc",
        "uuid_generate_v5",
        "nextval",
        "currval",
        "lastval",
        "now",
        "clock_timestamp",
        "statement_timestamp",
        "transaction_timestamp",
        "timeofday",
        "pg_sleep",
        "txid_current",
    }
)
_VOLATILE_FLAT = frozenset(v.replace("_", "") for v in VOLATILE_FUNCTIONS)

# Conservative deterministic allowlist (unqualified names, lowercase).
DETERMINISTIC_FUNCTIONS = frozenset(
    {
        "abs",
        "ceil",
        "ceiling",
        "floor",
        "round",
        "trunc",
        "power",
        "sqrt",
        "exp",
        "ln",
        "log",
        "coalesce",
        "nullif",
        "greatest",
        "least",
        "lower",
        "upper",
        "trim",
        "btrim",
        "ltrim",
        "rtrim",
        "substring",
        "substr",
        "replace",
        "concat",
        "concat_ws",
        "length",
        "char_length",
        "strpos",
        "split_part",
        "to_char",
        "to_number",
        "to_timestamp",
        "to_date",
        "date_part",
        "date_trunc",
        "extract",
        "cast",
        "case",
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
        "json_agg",
        "jsonb_agg",
    }
)
_ORDER_SENSITIVE_FLAT = frozenset(v.replace("_", "") for v in ORDER_SENSITIVE_FUNCTIONS)


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
    if tree.find(exp.Semicolon) is not None:
        raise SemanticError(ReasonCode.INTERNAL_ERROR, "terminal second statement")
    ctes: dict[str, Any] = {}
    # sqlglot>=~26 stores top-level CTEs under "with_"; older versions use "with".
    top_with = tree.args.get("with_")
    if top_with is None:
        top_with = tree.args.get("with")
    scope_ctes: list[Any] = []
    if top_with is not None:
        scope_ctes = top_with.args.get("expressions", []) or []
    for cte in scope_ctes:
        alias_expr = cte.args.get("alias")
        if alias_expr is None:
            continue
        alias_name = alias_expr.name if hasattr(alias_expr, "name") else str(alias_expr)
        if not alias_name:
            continue
        quoted = bool(alias_expr.args.get("quoted")) if hasattr(alias_expr, "args") else False
        ident = _normalize_ident(alias_name, quoted, _fold_for_dialect(dialect))
        ctes[ident] = cte
    return ParsedModel(sql=stripped, dialect=dialect, tree=tree, ctes=ctes)


_ALLOWED_PROJECTION_NODES = frozenset(
    {
        "Select",
        "Column",
        "Identifier",
        "Alias",
        "From",
        "Table",
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
        "Case",
        "Star",
    }
)

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


def _walk_types(node: Any) -> list[str]:
    return [type(n).__name__ for n in _iter_nodes(node)]


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
    if inner.find(exp.Anonymous) is not None or inner.find(exp.Func) is not None:
        # any function inside import is rejected (no scalar functions in direct import)
        found = inner.find(exp.Func)
        name = type(found).__name__ if found is not None else "Func"
        return Qualification(ok=False, reason_codes=(ReasonCode.UNSUPPORTED_IMPORT_SHAPE,), unexpected_nodes=(name,))
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
    tables = list(inner.find_all(exp.Table))
    if len(tables) != 1:
        return Qualification(
            ok=False,
            reason_codes=(ReasonCode.UNSUPPORTED_IMPORT_SHAPE,),
            unexpected_nodes=("TableCount",),
        )
    return Qualification(ok=True, reason_codes=(ReasonCode.OK,))


def _canonical(node: Any) -> Any:
    """Versioned canonical serializer: tuple form without positions/comments."""
    if node is None:
        return None
    if isinstance(node, list):
        return tuple(_canonical(v) for v in node)
    if isinstance(node, exp.Expression):
        name = type(node).__name__
        args: dict[str, Any] = {}
        for k, v in node.args.items():
            if k in ("comments", "meta"):
                continue
            args[k] = _canonical(v)
        if isinstance(node, exp.Identifier):
            quoted = bool(node.args.get("quoted"))
            nm = node.name
            args = {"this": nm if quoted else nm.lower(), "quoted": quoted}
        if isinstance(node, exp.Column):
            # normalize unquoted parts
            pass
        if isinstance(node, exp.Literal):
            args = {"this": node.this, "is_string": node.is_string}
        return (name, tuple(sorted(args.items())))
    return node


def canonical_fingerprint(node: Any, version: str = SEMANTIC_FINGERPRINT_VERSION) -> str:
    canon = _canonical(node)
    payload = json.dumps({"v": version, "ast": repr(canon)}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def predicate_fingerprint(cte_expr: Any) -> str | None:
    inner = cte_expr.args.get("this")
    if not isinstance(inner, exp.Select):
        return None
    where = inner.args.get("where")
    if where is None:
        return None
    return canonical_fingerprint(where)


def semantic_fingerprint(tree: Any) -> str:
    return canonical_fingerprint(tree)


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
        # confirm single input relation
        inner = compiled.args.get("this")
        assert isinstance(inner, exp.Select)
        tables = list(inner.find_all(exp.Table))
        if len(tables) != 1:
            raise SemanticError(ReasonCode.UNSUPPORTED_IMPORT_SHAPE, "compiled import must read one relation")
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
        fp = predicate_fingerprint(compiled)
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


def analyze_volatility(parsed: ParsedModel, deterministic_allowlist: frozenset[str] = frozenset()) -> VolatilityResult:
    func_names: list[str] = []
    for n in _iter_nodes(parsed.tree):
        if isinstance(n, exp.Anonymous):
            fname = str(n.this).lower() if n.this else "anonymous"
            func_names.append(fname)
        elif isinstance(n, exp.Func):
            func_names.append(type(n).__name__.lower())
            # also capture sql name
            try:
                sql_name = n.sql_name().lower()
                if sql_name not in func_names:
                    func_names.append(sql_name)
            except Exception:  # noqa: S112 -- best-effort display name only
                continue
    allow = {a.lower() for a in deterministic_allowlist}
    unknown: list[str] = []
    for fn in func_names:
        base = fn.split(".")[-1].lower()
        flat = base.replace("_", "")
        if base in VOLATILE_FUNCTIONS or flat in _VOLATILE_FLAT:
            return VolatilityResult(ok=False, reason_codes=(ReasonCode.NONDETERMINISTIC,), unknown_functions=(fn,))
        if base in ORDER_SENSITIVE_FUNCTIONS or flat in _ORDER_SENSITIVE_FLAT:
            return VolatilityResult(ok=False, reason_codes=(ReasonCode.NONDETERMINISTIC,), unknown_functions=(fn,))
        if base in allow or flat in allow:
            continue
        # known sqlglot function classes that are deterministic but not in list: treat common ones
        if base in (
            "eq",
            "neq",
            "gt",
            "gte",
            "lt",
            "lte",
            "and",
            "or",
            "not",
            "cast",
            "alias",
            "column",
            "select",
        ):
            continue
        unknown.append(fn)
    # sampling / limit checks
    sample_cls = getattr(exp, "Sample", None)
    if sample_cls is not None and parsed.tree.find(sample_cls) is not None:
        return VolatilityResult(ok=False, reason_codes=(ReasonCode.NONDETERMINISTIC,), unknown_functions=("sample",))
    limit = parsed.tree.find(exp.Limit)
    order = parsed.tree.find(exp.Order)
    if limit is not None and order is None:
        return VolatilityResult(
            ok=False,
            reason_codes=(ReasonCode.NONDETERMINISTIC,),
            unknown_functions=("unordered_limit",),
        )
    if unknown:
        return VolatilityResult(
            ok=False,
            reason_codes=(ReasonCode.NONDETERMINISTIC,),
            unknown_functions=tuple(sorted(set(unknown))),
        )
    return VolatilityResult(ok=True, reason_codes=(ReasonCode.OK,), unknown_functions=())


def validate_compiled_delta(baseline: ParsedModel, candidate: ParsedModel, expected_fingerprint: str) -> None:
    actual = semantic_fingerprint(candidate.tree)
    if actual != expected_fingerprint:
        raise SemanticError(ReasonCode.COMPILE_DRIFT, "candidate compiled AST differs from expected transform")


def build_expected_transform(
    baseline: ParsedModel,
    canonical_ident: str,
    donor_idents: tuple[str, ...],
    added_projections_sql: dict[str, list[str]],
    redirected_aliases: dict[str, str] | None = None,
) -> Any:
    """Deep-copy baseline AST, apply merge, return expected tree. Pure sqlglot transform."""
    import copy

    fold = _fold_for_dialect(baseline.dialect)
    tree = copy.deepcopy(baseline.tree)
    top_with = tree.args.get("with_")
    if top_with is None:
        top_with = tree.args.get("with")
    if top_with is None:
        raise SemanticError(ReasonCode.COMPILE_DRIFT, "baseline has no WITH")
    ctes = list(top_with.args.get("expressions", []) or [])

    def _cte_ident(c: object) -> str | None:
        alias_expr: Any = c.args.get("alias") if isinstance(c, exp.CTE) else None
        if alias_expr is None:
            return None
        q = bool(alias_expr.args.get("quoted")) if hasattr(alias_expr, "args") else False
        return _normalize_ident(alias_expr.name if hasattr(alias_expr, "name") else str(alias_expr), q, fold)

    by_ident = {}
    for c in ctes:
        key = _cte_ident(c)
        if key:
            by_ident[key] = c
    canonical = by_ident.get(canonical_ident)
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
    remaining = [c for c in ctes if (_cte_ident(c) not in set(donor_idents))]
    top_with.set("expressions", remaining)
    # redirect table refs bound to donors: rename table to canonical + alias donor
    canon_alias_expr = canonical.args.get("alias")
    canon_name = canon_alias_expr.name if hasattr(canon_alias_expr, "name") else str(canon_alias_expr)
    for scope_table in tree.find_all(exp.Table):
        # determine name
        tname = scope_table.name
        if _normalize_ident(tname, False, fold) in set(donor_idents):
            # preserve alias behavior: if table already aliased, keep alias; else add alias = donor
            existing_alias = scope_table.args.get("alias")
            donor_raw = tname
            # rename to canonical spelling (use canonical alias text from baseline)
            scope_table.set("this", exp.to_identifier(canon_name))
            if existing_alias is None:
                scope_table.set("alias", exp.to_identifier(donor_raw))
    _ = redirected_aliases
    return tree
