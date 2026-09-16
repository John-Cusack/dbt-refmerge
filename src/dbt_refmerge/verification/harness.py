"""PostgreSQL verification harness: project generation, preflight, view/compare/cleanup."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from dbt_refmerge.artifacts import ManifestNodeModel, load_manifest
from dbt_refmerge.domain import (
    IdentifierIdentity,
    ReasonCode,
    RelationIdentity,
    ScratchBoundary,
)
from dbt_refmerge.errors import ScratchBoundaryError, VerificationError
from dbt_refmerge.workspace import RunWorkspace

BASELINE_ALIAS_PREFIX = "dbt_refmerge_baseline_"
CANDIDATE_ALIAS_PREFIX = "dbt_refmerge_candidate_"

FORBIDDEN_SCHEMAS = {"pg_catalog", "information_schema", "pg_toast", "pg_temp_1"}


def normalize_pg_ident(text: str, quoted: bool) -> IdentifierIdentity:
    return IdentifierIdentity(value=text if quoted else text.lower())


def validate_scratch_schema(schema: str, model_schema: str | None = None) -> IdentifierIdentity:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", schema) and not (schema.startswith('"') and schema.endswith('"')):
        # allow quoted form "..." with escapes
        if not (len(schema) >= 2 and schema.startswith('"')):
            raise ScratchBoundaryError(f"invalid scratch schema: {schema!r}")
    quoted = schema.startswith('"')
    value = schema[1:-1].replace('""', '"') if quoted else schema
    ident = normalize_pg_ident(value, quoted)
    if ident.value.lower() in FORBIDDEN_SCHEMAS:
        raise ScratchBoundaryError(f"scratch schema forbidden: {schema}")
    if model_schema is not None and ident.value.lower() == model_schema.lower():
        raise ScratchBoundaryError("scratch schema must differ from model output schema")
    if "\x00" in schema or len(schema) > 128:
        raise ScratchBoundaryError("invalid scratch schema")
    return ident


def _sanitize_run_token(run_id: str) -> str:
    return re.sub(r"[^a-z0-9_]", "_", run_id.lower())[-16:]


HARNESS_MODELS_BASELINE = """{{ config(alias='%s', materialized='view') }}\n\n{%% raw %%}\n%s\n{%% endraw %%}\n"""

GENERATE_SCHEMA_NAME_MACRO = (
    "{% macro generate_schema_name(custom_schema_name, node) -%}\n    {{ target.schema }}\n{%- endmacro %}\n"
)

EMIT_SCHEMA_MACRO_TMPL = """{%% macro dbt_refmerge_emit_schema(baseline_ref, candidate_ref, nonce) %%}
  {%% set b_rel = ref('baseline') %%}
  {%% set c_rel = ref('candidate') %%}
  {%% set b_cols = adapter.get_columns_in_relation(b_rel) %%}
  {%% set c_cols = adapter.get_columns_in_relation(c_rel) %%}
  ...
{%% endmacro %%}
"""

COMPARE_MACRO_TMPL = """{%% macro dbt_refmerge_compare(nonce) %%}
  {%% set b = ref('baseline') %%}
  {%% set c = ref('candidate') %%}
  ...
{%% endmacro %%}
"""

CLEANUP_MACRO = """{% macro dbt_refmerge_cleanup() %}
  {% set b = ref('baseline') %}
  {% do adapter.drop_relation(b) %}
  {% set c = ref('candidate') %}
  {% do adapter.drop_relation(c) %}
{% endmacro %}
"""


@dataclass(frozen=True)
class HarnessSpec:
    baseline_alias: str
    candidate_alias: str
    baseline_sql: str
    candidate_sql: str


def strip_single_terminal_semicolon(sql: str) -> str:
    s = sql.rstrip()
    if s.endswith(";"):
        body = s[:-1]
        # ensure no second statement: no other semicolon outside strings/comments (approx: no ';' at all)
        if ";" in body.strip():
            raise VerificationError(ReasonCode.HARNESS_EMBEDDING_UNSAFE, "multiple statements in compiled SQL")
        return body
    if ";" in s:
        # interior semicolon -> unsafe
        raise VerificationError(ReasonCode.HARNESS_EMBEDDING_UNSAFE, "semicolon inside compiled SQL")
    return s


def build_harness_project(
    ws: RunWorkspace,
    *,
    profile: str,
    baseline_sql: str,
    candidate_sql: str,
) -> HarnessSpec:
    token = _sanitize_run_token(ws.run_id)
    baseline_alias = f"{BASELINE_ALIAS_PREFIX}{token}"
    candidate_alias = f"{CANDIDATE_ALIAS_PREFIX}{token}"
    for sql in (baseline_sql, candidate_sql):
        if "{% endraw %}" in sql:
            raise VerificationError(ReasonCode.HARNESS_EMBEDDING_UNSAFE, "raw-block terminator in compiled SQL")
    baseline_body = strip_single_terminal_semicolon(baseline_sql)
    candidate_body = strip_single_terminal_semicolon(candidate_sql)
    root = ws.harness_project
    (root / "models").mkdir(parents=True, exist_ok=True)
    (root / "macros").mkdir(parents=True, exist_ok=True)
    (root / "dbt_project.yml").write_text(
        f'name: dbt_refmerge_harness\nversion: 1.0.0\nconfig-version: 2\nprofile: "{profile}"\n\n'
        'model-paths: ["models"]\nmacro-paths: ["macros"]\ntarget-path: "target"\nclean-targets: ["target"]\n\n'
        "models:\n  dbt_refmerge_harness:\n    +materialized: view\n",
        encoding="utf-8",
    )
    (root / "macros" / "generate_schema_name.sql").write_text(GENERATE_SCHEMA_NAME_MACRO, encoding="utf-8")
    (root / "macros" / "cleanup_relations.sql").write_text(CLEANUP_MACRO, encoding="utf-8")
    (root / "models" / "baseline.sql").write_text(
        HARNESS_MODELS_BASELINE % (baseline_alias, baseline_body), encoding="utf-8"
    )
    (root / "models" / "candidate.sql").write_text(
        HARNESS_MODELS_BASELINE % (candidate_alias, candidate_body), encoding="utf-8"
    )
    return HarnessSpec(
        baseline_alias=baseline_alias,
        candidate_alias=candidate_alias,
        baseline_sql=baseline_body,
        candidate_sql=candidate_body,
    )


def preflight_harness_manifest(
    manifest_path: Path,
    boundary: ScratchBoundary,
) -> tuple[RelationIdentity, RelationIdentity]:
    view = load_manifest(manifest_path)
    wanted = {"model.dbt_refmerge_harness.baseline", "model.dbt_refmerge_harness.candidate"}
    nodes = {uid: view.nodes[uid] for uid in wanted if uid in view.nodes}
    if set(nodes) != wanted:
        raise ScratchBoundaryError("harness manifest must contain exactly baseline/candidate nodes")
    allowed = {r.identifier.value for r in boundary.allowed_relations}
    out: list[RelationIdentity] = []
    for uid in sorted(wanted):
        node: ManifestNodeModel = nodes[uid]
        if node.config.get("materialized", "view") != "view":
            raise ScratchBoundaryError(f"harness node not a view: {uid}")
        for hook in ("pre-hook", "post-hook", "pre_hook", "post_hook"):
            if node.config.get(hook):
                raise ScratchBoundaryError(f"harness hooks forbidden: {uid}")
        db = normalize_pg_ident(node.database or "", False)
        schema = normalize_pg_ident(node.schema_ or "", False)
        alias_ident = normalize_pg_ident(node.alias or "", False)
        if db != boundary.database or schema != boundary.schema:
            raise ScratchBoundaryError(f"harness node outside scratch boundary: {uid}")
        if alias_ident.value not in allowed:
            raise ScratchBoundaryError(f"harness alias not allowlisted: {uid}")
        out.append(RelationIdentity(database=db, schema=schema, identifier=alias_ident))
    # reject extra enabled writable nodes
    for uid, node in view.nodes.items():
        if uid not in wanted and node.resource_type in ("model", "seed", "snapshot"):
            cfg = node.config if isinstance(node.config, dict) else {}
            if cfg.get("enabled", True):
                raise ScratchBoundaryError(f"unexpected writable node: {uid}")
    return (out[0], out[1])


def build_verdict_sql(baseline_quoted: str, candidate_quoted: str, columns: list[str], strategy: str) -> str:
    from dbt_refmerge.verification.comparator import (
        generate_except_all_sql,
        generate_grouped_counts_sql,
        quote_ident,
    )

    # columns are raw catalog names; quoting handled inside generators
    _ = quote_ident
    if strategy == "grouped_counts":
        return generate_grouped_counts_sql(baseline_quoted, candidate_quoted, columns)
    return generate_except_all_sql(baseline_quoted, candidate_quoted, columns)


def ledger_relations(ws: RunWorkspace) -> list[RelationIdentity]:
    data = json.loads(ws.ledger_path.read_text())
    out: list[RelationIdentity] = []
    for o in data.get("objects", []):
        out.append(
            RelationIdentity(
                database=IdentifierIdentity(o["database"]),
                schema=IdentifierIdentity(o["schema"]),
                identifier=IdentifierIdentity(o["identifier"]),
            )
        )
    return out
