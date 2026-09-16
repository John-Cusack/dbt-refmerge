"""Exact multiset comparator: schema gate, macro generation, result parsing."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Protocol

from dbt_refmerge.domain import EqualityResult, ReasonCode
from dbt_refmerge.errors import VerificationError


@dataclass(frozen=True)
class ColumnSchema:
    ordinal: int
    name: str
    data_type: str


@dataclass(frozen=True)
class RelationSchema:
    columns: tuple[ColumnSchema, ...]


@dataclass(frozen=True)
class RawSchemaPayload:
    baseline: list[dict[str, Any]]
    candidate: list[dict[str, Any]]


def _normalize_type(t: str) -> str:
    s = re.sub(r"\s+", " ", t.strip().lower())
    aliases = {
        "int": "integer",
        "int4": "integer",
        "int2": "smallint",
        "int8": "bigint",
        "varchar": "character varying",
        "char": "character",
        "timestamptz": "timestamp with time zone",
    }
    # strip precision details for numeric/varchar but keep exact reporting: keep as-is except spacing
    return aliases.get(s, s)


class Comparator(Protocol):
    def normalize_schema(self, raw: RawSchemaPayload) -> RelationSchema: ...
    def validate_types(self, schema: RelationSchema, supported: frozenset[str]) -> tuple[ReasonCode, ...]: ...
    def generate_macro(self, schema: RelationSchema, strategy: str, result_nonce: str) -> bytes: ...
    def parse_result(self, output: str, result_nonce: str) -> EqualityResult: ...


def normalize_schema(raw: RawSchemaPayload) -> RelationSchema:
    # baseline authoritative for column order; candidate compared separately
    cols: list[ColumnSchema] = []
    for entry in raw.baseline:
        cols.append(
            ColumnSchema(
                ordinal=int(entry["ordinal"]),
                name=str(entry["name"]),
                data_type=_normalize_type(str(entry["data_type"])),
            )
        )
    return RelationSchema(columns=tuple(cols))


def schemas_equal(baseline: RelationSchema, candidate_raw: RawSchemaPayload) -> bool:
    cand = [
        (str(e["name"]), _normalize_type(str(e["data_type"])))
        for e in sorted(candidate_raw.candidate, key=lambda e: int(e["ordinal"]))
    ]
    base = [(c.name, c.data_type) for c in baseline.columns]
    if len(cand) != len(base):
        return False
    for (bn, bt), (cn, ct) in zip(base, cand, strict=True):
        # ordered identifiers: postgres unquoted folds to lower; quoted exact.
        # v0.1: exact name match (catalog attname is already resolved spelling)
        if bn != cn or bt != ct:
            return False
    return True


def validate_types(schema: RelationSchema, supported: frozenset[str]) -> tuple[ReasonCode, ...]:
    bad: list[ReasonCode] = []
    for col in schema.columns:
        base = col.data_type.split("(")[0].strip()
        if base not in supported and col.data_type not in supported:
            return (ReasonCode.UNSUPPORTED_COMPARISON_TYPE,)
    return (ReasonCode.OK,) if not bad else tuple(bad)


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def generate_except_all_sql(baseline_ref: str, candidate_ref: str, columns: list[str]) -> str:
    cols = ", ".join(quote_ident(c) for c in columns)
    return (
        "with\n"
        f"a as (\n    select {cols} from {baseline_ref}\n),\n"
        f"b as (\n    select {cols} from {candidate_ref}\n),\n"
        "a_minus_b as (\n    select * from a\n    except all\n    select * from b\n),\n"
        "b_minus_a as (\n    select * from b\n    except all\n    select * from a\n)\n"
        "select\n"
        "    (select count(*) from a) as baseline_rows,\n"
        "    (select count(*) from b) as candidate_rows,\n"
        "    (select count(*) from a_minus_b) as baseline_only_occurrences,\n"
        "    (select count(*) from b_minus_a) as candidate_only_occurrences"
    )


def generate_grouped_counts_sql(baseline_ref: str, candidate_ref: str, columns: list[str]) -> str:
    cols = ", ".join(quote_ident(c) for c in columns)
    marker = "__dbt_refmerge_side"
    # ensure collision-free
    suffix = ""
    while marker + suffix in columns:
        suffix += "_"
    side = marker + suffix
    group_cols = cols
    return (
        "with\n"
        f"a as (\n    select {cols}, 1 as {quote_ident(side)} from {baseline_ref}\n),\n"
        f"b as (\n    select {cols}, -1 as {quote_ident(side)} from {candidate_ref}\n),\n"
        f"u as (\n    select * from a\n    union all\n    select * from b\n),\n"
        f"g as (\n    select {group_cols}, sum({quote_ident(side)}) as _delta,\n"
        f"           sum(case when {quote_ident(side)} = 1 then 1 else 0 end) as _a,\n"
        f"           sum(case when {quote_ident(side)} = -1 then 1 else 0 end) as _b\n"
        f"    from u group by {group_cols}\n)\n"
        "select\n"
        "    (select coalesce(sum(_a),0) from g) as baseline_rows,\n"
        "    (select coalesce(sum(_b),0) from g) as candidate_rows,\n"
        "    (select coalesce(sum(case when _delta > 0 then _delta else 0 end),0)\n"
        "    from g) as baseline_only_occurrences,\n"
        "    (select coalesce(sum(case when _delta < 0 then -_delta else 0 end),0)\n"
        "    from g) as candidate_only_occurrences"
    )


def parse_marked_json(output: str, result_nonce: str) -> dict[str, Any]:
    begin = f"DBT_REFMERGE_RESULT_{result_nonce}_BEGIN"
    end = f"DBT_REFMERGE_RESULT_{result_nonce}_END"
    if output.count(begin) != 1 or output.count(end) != 1:
        raise VerificationError(ReasonCode.DBT_COMMAND_FAILED, "missing/duplicate result markers")
    inner = output.split(begin, 1)[1].split(end, 1)[0].strip()
    lines = [ln for ln in inner.splitlines() if ln.strip()]
    if len(lines) != 1:
        raise VerificationError(ReasonCode.DBT_COMMAND_FAILED, "result payload must be single-line JSON")
    if len(inner.encode("utf-8")) > 1_000_000:
        raise VerificationError(ReasonCode.DBT_COMMAND_FAILED, "result payload oversized")
    try:
        payload: dict[str, Any] = json.loads(lines[0])
    except json.JSONDecodeError as exc:
        raise VerificationError(ReasonCode.DBT_COMMAND_FAILED, f"malformed result JSON: {exc}") from exc
    return payload


def parse_equality_result(payload: dict[str, Any]) -> EqualityResult:
    try:
        schema_equal = bool(payload["schema_equal"])
        b = payload["baseline_rows"]
        c = payload["candidate_rows"]
        bo = payload["baseline_only_occurrences"]
        co = payload["candidate_only_occurrences"]
    except KeyError as exc:
        raise VerificationError(ReasonCode.DBT_COMMAND_FAILED, f"missing result field: {exc}") from exc
    for name, val in (
        ("baseline_rows", b),
        ("candidate_rows", c),
        ("baseline_only_occurrences", bo),
        ("candidate_only_occurrences", co),
    ):
        if isinstance(val, bool) or not isinstance(val, int) or val < 0 or val > 2**63 - 1:
            raise VerificationError(ReasonCode.DBT_COMMAND_FAILED, f"invalid count field {name}: {val!r}")
    extra = set(payload) - {
        "schema_equal",
        "baseline_rows",
        "candidate_rows",
        "baseline_only_occurrences",
        "candidate_only_occurrences",
    }
    if extra:
        raise VerificationError(ReasonCode.DBT_COMMAND_FAILED, f"extra result fields: {sorted(extra)}")
    # logical consistency: if directional differences are zero, row counts must agree
    if bo == 0 and co == 0 and b != c:
        raise VerificationError(ReasonCode.DBT_COMMAND_FAILED, "inconsistent counts: zero diff but unequal totals")
    return EqualityResult(
        schema_equal=schema_equal,
        baseline_rows=b,
        candidate_rows=c,
        baseline_only_occurrences=bo,
        candidate_only_occurrences=co,
    )


def derive_status(equality: EqualityResult) -> str:
    from dbt_refmerge.domain import VerificationStatus

    if not equality.schema_equal:
        return VerificationStatus.DIFFERENT
    if equality.baseline_only_occurrences == 0 and equality.candidate_only_occurrences == 0:
        return VerificationStatus.SNAPSHOT_EQUIVALENT
    return VerificationStatus.DIFFERENT
