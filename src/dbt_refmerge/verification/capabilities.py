"""Adapter capabilities + PostgreSQL exact-type registry."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class VerificationCapabilities:
    adapter_type: str
    sqlglot_dialect: str
    bag_strategy: str  # "except_all" | "grouped_counts"
    scratch_relation_type: str  # "view"
    statement_snapshot_supported: bool
    supported_exact_types: frozenset[str]
    query_cost_controls: frozenset[str]


POSTGRES_EXACT_TYPES = frozenset(
    {
        "boolean",
        "smallint",
        "integer",
        "bigint",
        "numeric",
        "text",
        "character varying",
        "character",
        "varchar",
        "char",
        "date",
        "timestamp without time zone",
        "timestamp with time zone",
        "uuid",
        "bytea",
    }
)

POSTGRES_CAPABILITIES = VerificationCapabilities(
    adapter_type="postgres",
    sqlglot_dialect="postgres",
    bag_strategy="except_all",
    scratch_relation_type="view",
    statement_snapshot_supported=True,
    supported_exact_types=POSTGRES_EXACT_TYPES,
    query_cost_controls=frozenset({"statement_timeout"}),
)

SUPPORTED_ADAPTERS = ("postgres",)

COMPARATOR_VERSION = "1"
