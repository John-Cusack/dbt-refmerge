"""Conservative column demand analysis and byte edits for direct imports.

No warehouse schema is needed: a wildcard can be narrowed only when every
consumer names its inputs, or forwards a single source's wildcard to such a
consumer. Ambiguous or schema-dependent SQL leaves the model alone.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from sqlglot import exp

from dbt_refmerge.domain import ReasonCode, SourceSpan, TextEdit
from dbt_refmerge.errors import RefmergeError, SemanticError
from dbt_refmerge.semantics import (
    ParsedModel,
    _cte_identity,
    _fold_for_dialect,
    _normalize_ident,
    _top_level_with,
    parse_model,
)
from dbt_refmerge.source import ParsedSourceModel


@dataclass(frozen=True)
class PrunedImport:
    cte_identity: str
    original_projections: tuple[str, ...]
    projections: tuple[str, ...]
    edit: TextEdit


class _Unsupported(Exception):
    """The source does not give us enough information to narrow its imports."""


def _identity(node: exp.Expression, fold: str) -> str:
    if fold == "insensitive":
        return node.name.lower()
    return _normalize_ident(node.name, bool(node.args.get("quoted")), fold)


def _wildcard(query: exp.Select) -> bool:
    return any(projection.is_star for projection in query.expressions)


def _local_query(query: exp.Select) -> exp.Select:
    local = query.copy()
    local.set("with_", None)
    local.set("with", None)
    return local


def _excluded_columns(wildcard: exp.Expression, dialect: str, fold: str) -> set[str]:
    excluded: set[str] = set()
    for modifier, value in wildcard.args.items():
        if not value:
            continue
        # SQLGlot 26 uses "except"; newer versions use "except_".
        if modifier not in ("except", "except_") or dialect not in ("snowflake", "bigquery"):
            raise _Unsupported
        for column in value:
            if not isinstance(column, exp.Column) or len(column.parts) != 1 or column.is_star:
                raise _Unsupported
            excluded.add(_identity(column.this, fold))
    return excluded


def _row_source_alias(relation: exp.Expression, dialect: str) -> exp.Identifier:
    alias = relation.args.get("alias")
    if alias is not None:
        if (
            dialect == "bigquery"
            and isinstance(relation, exp.Unnest)
            and len(relation.expressions) == 1
            and "offset" in relation.args  # explicit UNNEST; implicit paths can be ambiguous
            and not relation.args.get("offset")
            and len(alias.columns) == 1
        ):
            return cast(exp.Identifier, alias.columns[0])
        if (
            dialect == "snowflake"
            and isinstance(relation, exp.Lateral)
            and isinstance(relation.this, exp.Explode)
            and isinstance(alias.this, exp.Identifier)
        ):
            return alias.this
    raise _Unsupported


def plan_pruning(model: ParsedSourceModel, dialect: str) -> tuple[PrunedImport, ...]:
    """Find imports to narrow; unsupported analysis returns no edits."""
    if dialect == "bigquery" and any(
        token.text.startswith("`") and ("\\" in token.text or "." in token.text) for token in model.tokens
    ):
        # SQLGlot can split a single quoted dotted identifier into a field path,
        # and does not consistently decode BigQuery identifier escape sequences.
        return ()
    first_cte = model.ctes[0].cte_span.start_byte if model.ctes else 0
    ref_spans = {call.span for call in model.masked.ref_calls.values()}
    for jinja in model.masked.jinja_spans:
        if jinja.span in ref_spans or jinja.kind == "comment":
            continue
        # dbt config is allowed before SQL; SQL-producing macros/control flow are not.
        if (
            jinja.span.end_byte <= first_cte
            and jinja.kind == "expression"
            and jinja.text.lstrip(b"{ -\t\r\n").startswith(b"config(")
        ):
            continue
        return ()
    try:
        parsed = parse_model(model.masked.masked_text, dialect)
        return _plan(model, parsed)
    except (RefmergeError, _Unsupported):
        return ()


def _plan(model: ParsedSourceModel, parsed: ParsedModel) -> tuple[PrunedImport, ...]:
    # BigQuery column names and query aliases remain case-insensitive when quoted.
    fold = "insensitive" if parsed.dialect == "bigquery" else model.fold_unquoted
    if (
        not isinstance(parsed.tree, exp.Select)
        or _wildcard(parsed.tree)
        or model.nested_names
        or parsed.tree.find(exp.Pivot) is not None
    ):
        return ()
    analysis_ctes = {_identity(cte.args["alias"].this, fold): cte for cte in parsed.ctes.values()}
    source_ctes = {
        (cte.identifier.identity.value.lower() if fold == "insensitive" else cte.identifier.identity.value): cte
        for cte in model.ctes
    }
    if parsed.dialect != "bigquery" and set(parsed.ctes) != set(source_ctes):
        return ()
    if (
        len(analysis_ctes) != len(parsed.ctes)
        or len(source_ctes) != len(model.ctes)
        or set(analysis_ctes) != set(source_ctes)
    ):
        return ()
    queries: list[tuple[str | None, exp.Select]] = []
    for cte_name, cte in analysis_ctes.items():
        if not isinstance(cte.this, exp.Select) or cte.alias_column_names:
            return ()
        queries.append((cte_name, _local_query(cte.this)))
    # Nested queries, set operations and CTE column lists need schema-aware binding.
    if len(list(parsed.tree.find_all(exp.Select))) != len(model.ctes) + 1:
        return ()
    queries.append((None, _local_query(parsed.tree)))
    # Dicts retain the first spelling/use of each column, including quoted names.
    demands: dict[str, dict[str, exp.Identifier]] = {name: {} for name in analysis_ctes}
    result: list[PrunedImport] = []
    available = set(analysis_ctes)
    for query_name, query in reversed(queries):
        name = query_name
        if name is not None:
            available.remove(name)
        if query.args.get("kind") or query.args.get("operation_modifiers"):
            raise _Unsupported
        from_clause = query.args.get("from_") or query.args.get("from")
        relations = ([from_clause.this] if from_clause is not None else []) + [
            join.this for join in query.args.get("joins", [])
        ]
        tables = list(query.find_all(exp.Table))
        sources: dict[str, str | None] = {}
        for relation in relations:
            if isinstance(relation, exp.Table):
                if not isinstance(relation.this, exp.Identifier) or relation.alias_column_names:
                    raise _Unsupported
                alias = relation.args.get("alias")
                alias_ident = alias.this if alias else relation.this
                target = _identity(relation.this, fold)
                if relation.db or relation.catalog:
                    target = ""
                elif target in analysis_ctes and target not in available:
                    raise _Unsupported
            else:
                alias_ident = _row_source_alias(relation, parsed.dialect)
                target = ""  # derived rows are bound, but are not editable imports
            key = _identity(alias_ident, fold)
            if key in sources:
                raise _Unsupported
            sources[key] = target if target in available else None

        def require(column: exp.Identifier, targets: list[str | None]) -> None:
            for target in targets:
                if target is not None:
                    demands[target].setdefault(_identity(column, fold), column.copy())

        star = _wildcard(query)
        if star:
            if (
                len(query.expressions) != 1
                or len(sources) != 1
                or query.args.get("distinct")
                or query.args.get("group")
                or query.args.get("having")
            ):
                raise _Unsupported
            projection = query.expressions[0]
            wildcard = projection.this if isinstance(projection, exp.Column) else projection
            excluded = _excluded_columns(wildcard, parsed.dialect, fold)
            if isinstance(projection, exp.Column) and _identity(projection.args["table"], fold) not in sources:
                raise _Unsupported
            # Ordinals refer to the unknown original wildcard order.
            for clause in (query.args.get("order"), query.args.get("group")):
                if clause is not None and any(isinstance(node, exp.Literal) for node in clause.find_all(exp.Literal)):
                    raise _Unsupported
            if name is None:  # root wildcards were already excluded
                raise _Unsupported  # pragma: no cover
            if excluded.intersection(demands[name]):
                raise _Unsupported
            for column in demands[name].values():
                require(column, list(sources.values()))

        using: dict[str, exp.Identifier] = {}
        for join in query.args.get("joins", []):
            if str(join.args.get("method", "")).upper() == "NATURAL":
                raise _Unsupported
            for column in join.args.get("using") or []:
                using[_identity(column, fold)] = column
                require(column, list(sources.values()))
        aliases = {_identity(p.args["alias"], fold) for p in query.expressions if isinstance(p, exp.Alias)}
        for column_ref in query.find_all(exp.Column):
            if column_ref.find_ancestor(exp.Star):
                # A downstream EXCEPT/EXCLUDE still needs the excluded column to
                # exist in its input, even though it omits it from its output.
                require(column_ref.this, list(sources.values()))
                continue
            if column_ref.is_star:
                if column_ref not in query.expressions:
                    raise _Unsupported  # a whole-row value such as COUNT(t.*)
                continue
            if len(column_ref.parts) > 2:
                # BigQuery's alias.struct.field syntax names the top-level struct
                # column explicitly. Preserve that entire column, not just a field.
                if parsed.dialect == "bigquery" and _identity(column_ref.parts[0], fold) in sources:
                    require(cast(exp.Identifier, column_ref.parts[1]), [sources[_identity(column_ref.parts[0], fold)]])
                    continue
                raise _Unsupported
            ident = column_ref.this
            key = _identity(ident, fold)
            if column_ref.table:
                table_key = _identity(column_ref.args["table"], fold)
                if table_key not in sources:
                    raise _Unsupported
                require(ident, [sources[table_key]])
            else:
                if key in sources:  # whole-row references: SELECT t FROM t
                    raise _Unsupported
                if key in aliases and column_ref.find_ancestor(exp.Order, exp.Group, exp.Having, exp.Qualify):
                    # ORDER BY aliases have well-defined output binding; other clauses vary by dialect.
                    ancestor = column_ref.find_ancestor(exp.Order, exp.Group, exp.Having, exp.Qualify)
                    if isinstance(ancestor, exp.Order) and ancestor.parent is query:
                        continue
                    raise _Unsupported
                if parsed.dialect == "snowflake" and key in aliases:
                    owner = column_ref.find_ancestor(exp.Alias)
                    if owner is None or _identity(owner.args["alias"], fold) != key:
                        # Snowflake can reuse SELECT aliases in other expressions
                        # and WHERE; a wildcard hides possible input-name collisions.
                        raise _Unsupported
                if len(sources) > 1 and key not in using:
                    raise _Unsupported
                require(ident, list(sources.values()))
        for wildcard in query.find_all(exp.Star):
            if wildcard in query.expressions or (isinstance(wildcard.parent, exp.Column) and wildcard.parent.is_star):
                continue
            if isinstance(wildcard.parent, exp.Count) and wildcard.parent.this is wildcard:
                continue
            raise _Unsupported

        if name is None or not demands[name]:
            continue
        source_cte = source_ctes[name]
        # Only a direct literal ref()/source() import can be edited.
        if len(tables) != 1 or tables[0].name not in model.masked.ref_calls:
            continue
        if not star and source_cte.ref_call is None:
            continue
        tokens = [
            token
            for token in model.tokens
            if token.kind not in ("space", "comment")
            and source_cte.body_span.start_byte <= model.decoded.char_to_byte[token.start]
            and model.decoded.char_to_byte[token.end] <= source_cte.body_span.end_byte
        ]
        from_index = next(i for i, token in enumerate(tokens) if token.kind == "word" and token.text.upper() == "FROM")
        if star:
            column_tokens = tokens[1:from_index]
            if not (
                column_tokens[0].text == "*"
                or (len(column_tokens) >= 3 and [token.text for token in column_tokens[1:3]] == [".", "*"])
            ):
                raise _Unsupported  # SELECT ALL, TOP, etc. are not part of the projection.
        projection_span = SourceSpan(
            model.decoded.char_to_byte[tokens[1].start], model.decoded.char_to_byte[tokens[from_index - 1].end]
        )
        if any(
            token.kind == "comment" and tokens[0].end <= token.start < tokens[from_index].start
            for token in model.tokens
        ) or any(
            jinja.kind == "comment"
            and model.decoded.char_to_byte[tokens[0].end]
            <= jinja.span.start_byte
            < model.decoded.char_to_byte[tokens[from_index].start]
            for jinja in model.masked.jinja_spans
        ):
            continue
        if star:
            projections = tuple(
                exp.Column(this=column.copy()).sql(dialect=parsed.dialect) for column in demands[name].values()
            )
            fragments = tuple(projection.encode("utf-8") for projection in projections)
        else:
            kept = [
                (projection, ast)
                for projection, ast in zip(source_cte.projections, query.expressions, strict=True)
                if (
                    projection.output_identifier.identity.value.lower()
                    if fold == "insensitive"
                    else projection.output_identifier.identity.value
                )
                in demands[name]
            ]
            if not kept or len(kept) == len(query.expressions):
                continue
            projections = tuple(ast.sql(dialect=parsed.dialect) for _, ast in kept)
            fragments = tuple(
                model.decoded.original_bytes[projection.span.start_byte : projection.span.end_byte]
                for projection, _ in kept
            )
        original = model.decoded.original_bytes[projection_span.start_byte : projection_span.end_byte]
        separator = b", "
        if b"\n" in original:
            from dbt_refmerge.rewrite import _detect_newline_indent

            newline, indent, _ = _detect_newline_indent(original)
            separator = b"," + newline + indent
        replacement = separator.join(fragments)
        if original.rstrip().endswith(b","):
            replacement += b","
        result.append(
            PrunedImport(
                source_cte.identifier.identity.value,
                tuple(projection.sql(dialect=parsed.dialect) for projection in query.expressions),
                projections,
                TextEdit(projection_span, replacement, ReasonCode.OK),
            )
        )
    return tuple(reversed(result))


def apply_expected_pruning(parsed: ParsedModel, prunings: tuple[PrunedImport, ...]) -> ParsedModel:
    """Apply only the planned projection changes to the baseline compiled AST."""
    tree = parsed.tree.copy()
    top_with = _top_level_with(tree)
    ctes = {
        _cte_identity(cte, _fold_for_dialect(parsed.dialect)): cte
        for cte in (top_with.expressions if top_with is not None else [])
    }
    # Keep untouched syntax in the original AST. Rendering/reparsing can change
    # Snowflake VARIANT paths and cast types, producing false compile drift.
    expected = ParsedModel(sql=parsed.sql, dialect=parsed.dialect, tree=tree, ctes=ctes)
    for pruning in prunings:
        cte = expected.ctes.get(pruning.cte_identity)
        if cte is None or not isinstance(cte.this, exp.Select):
            raise SemanticError(ReasonCode.SOURCE_MAPPING_AMBIGUOUS, "pruned import is absent from compiled SQL")
        original = tuple(projection.sql(dialect=parsed.dialect) for projection in cte.this.expressions)
        if original != pruning.original_projections:
            raise SemanticError(ReasonCode.SOURCE_MAPPING_AMBIGUOUS, "pruned import projections differ in compiled SQL")
        cte.this.set(
            "expressions", [exp.maybe_parse(projection, dialect=parsed.dialect) for projection in pruning.projections]
        )
    return expected
