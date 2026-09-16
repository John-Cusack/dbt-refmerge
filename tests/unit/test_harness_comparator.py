"""Artifacts, harness, comparator tests."""

import json
from pathlib import Path

import pytest

from dbt_refmerge.artifacts import load_manifest
from dbt_refmerge.domain import ReasonCode
from dbt_refmerge.errors import ArtifactError, ScratchBoundaryError, VerificationError
from dbt_refmerge.verification import comparator as comp
from dbt_refmerge.verification.harness import (
    build_harness_project,
    strip_single_terminal_semicolon,
    validate_scratch_schema,
)
from dbt_refmerge.workspace import RunWorkspace


def _write_manifest(tmp_path: Path, schema: str) -> Path:
    payload = {
        "metadata": {"dbt_schema_version": schema, "dbt_version": "1.8.0"},
        "nodes": {},
        "sources": {},
    }
    p = tmp_path / "manifest.json"
    p.write_text(json.dumps(payload))
    return p


def test_supported_manifest_loads(tmp_path):
    p = _write_manifest(tmp_path, "https://schemas.getdbt.com/dbt/manifest/v12.json")
    view = load_manifest(p)
    assert view.metadata.dbt_version == "1.8.0"


def test_unknown_manifest_rejected(tmp_path):
    p = _write_manifest(tmp_path, "https://schemas.getdbt.com/dbt/manifest/v99.json")
    with pytest.raises(ArtifactError) as ei:
        load_manifest(p)
    assert ei.value.reason_code == ReasonCode.UNSUPPORTED_MANIFEST_SCHEMA


def test_comparator_counts_and_markers():
    payload = {
        "schema_equal": True,
        "baseline_rows": 3,
        "candidate_rows": 3,
        "baseline_only_occurrences": 0,
        "candidate_only_occurrences": 0,
    }
    out = "logs\nDBT_REFMERGE_RESULT_n_BEGIN\n" + json.dumps(payload) + "\nDBT_REFMERGE_RESULT_n_END\ndone"
    parsed = comp.parse_marked_json(out, "n")
    eq = comp.parse_equality_result(parsed)
    assert eq.baseline_rows == 3 and comp.derive_status(eq) == "snapshot_equivalent"


def test_comparator_rejects_inconsistent():
    with pytest.raises(Exception):
        comp.parse_equality_result(
            {
                "schema_equal": True,
                "baseline_rows": 2,
                "candidate_rows": 3,
                "baseline_only_occurrences": 0,
                "candidate_only_occurrences": 0,
            }
        )


def test_verdict_sql_is_one_statement():
    sql = comp.generate_except_all_sql('"db"."s"."b"', '"db"."s"."c"', ["a", "b"])
    assert "except all" in sql.lower()
    grouped = comp.generate_grouped_counts_sql('"db"."s"."b"', '"db"."s"."c"', ["a"])
    assert "union all" in grouped.lower()


def test_harness_rejects_endraw(tmp_path):
    ws = RunWorkspace("20260101T000000_abcdef123456", tmp_path)
    with pytest.raises(Exception):
        build_harness_project(ws, profile="p", baseline_sql="select 1 {% endraw %}", candidate_sql="select 1")


def test_semicolon_rules():
    assert strip_single_terminal_semicolon("select 1;") == "select 1"
    with pytest.raises(Exception):
        strip_single_terminal_semicolon("select 1; select 2")


def _payload(**overrides):
    payload = {
        "schema_equal": True,
        "baseline_rows": 1,
        "candidate_rows": 1,
        "baseline_only_occurrences": 0,
        "candidate_only_occurrences": 0,
    }
    payload.update(overrides)
    return payload


@pytest.mark.parametrize("schema_equal", ["false", "true", 1, 0, None])
def test_parse_equality_result_requires_boolean_schema_equal(schema_equal):
    # S2: bool("false") is True; schema equality is the only signal for type-only changes.
    with pytest.raises(VerificationError) as exc_info:
        comp.parse_equality_result(_payload(schema_equal=schema_equal))
    assert exc_info.value.reason_code is ReasonCode.DBT_COMMAND_FAILED


@pytest.mark.parametrize(
    "candidate_sql",
    ["select 1 {%endraw%}", "select 1 {%- endraw %}", "select 1 {% endraw -%}", "select 1 {%-endraw-%}"],
)
def test_harness_rejects_every_raw_block_terminator(tmp_path, candidate_sql):
    # S3: Jinja closes {% raw %} on any whitespace-control variant of endraw.
    ws = RunWorkspace("20260101T000000_abcdef123456", tmp_path)
    with pytest.raises(VerificationError) as exc_info:
        build_harness_project(ws, profile="p", baseline_sql="select 1", candidate_sql=candidate_sql)
    assert exc_info.value.reason_code is ReasonCode.HARNESS_EMBEDDING_UNSAFE
    assert not (tmp_path / "harness_project" / "models").exists()


@pytest.mark.parametrize(
    "schema",
    ['"abc', 'abc"', '""', '"a"b"', "a" * 64, '"' + "\u00e9" * 32 + '"', "a-b", "pg_catalog"],
    ids=[
        "open-quote",
        "close-quote",
        "empty-quoted",
        "bare-inner-quote",
        "64-bytes",
        "64-utf8-bytes",
        "dash",
        "forbidden",
    ],
)
def test_validate_scratch_schema_rejects(schema):
    # S4 (Postgres truncates identifiers at 63 bytes) and S5 (malformed quoted identifiers).
    with pytest.raises(ScratchBoundaryError):
        validate_scratch_schema(schema)


@pytest.mark.parametrize(
    ("schema", "expected"),
    [("Scratch", "scratch"), ('"Scr""x"', 'Scr"x'), ("a" * 63, "a" * 63), ('"' + "\u00e9" * 31 + '"', "\u00e9" * 31)],
    ids=["unquoted-folds", "quoted-escape", "63-bytes", "62-utf8-bytes"],
)
def test_validate_scratch_schema_accepts(schema, expected):
    assert validate_scratch_schema(schema).value == expected


@pytest.mark.parametrize("generate", [comp.generate_except_all_sql, comp.generate_grouped_counts_sql])
@pytest.mark.parametrize("relation", ["a", "s.a", '"s"."a"', '"db"."s"."a"; drop table x'])
def test_verdict_sql_requires_quoted_three_part_relations(generate, relation):
    # S6: an unqualified relation named like a verdict CTE would compare a relation with itself.
    with pytest.raises(VerificationError):
        generate(relation, '"db"."s"."c"', ["x"])


@pytest.mark.parametrize("generate", [comp.generate_except_all_sql, comp.generate_grouped_counts_sql])
def test_verdict_sql_names_cannot_collide_with_user_names(generate):
    import sqlglot
    import sqlglot.expressions as exp

    columns = ["a", "b", "u", "g", "_a", "_b", "_delta", "__dbt_refmerge_side"]
    sql = generate('"db"."s"."a"', '"db"."s"."b"', columns)
    tree = sqlglot.parse_one(sql, read="postgres")
    cte_names = {cte.alias for cte in tree.find_all(exp.CTE)}
    assert cte_names and not cte_names & {"a", "b"}
    for cte in tree.find_all(exp.CTE):
        aliases = [projection.alias_or_name for projection in cte.this.expressions]
        assert len(aliases) == len(set(aliases)), cte.alias


@pytest.mark.parametrize("path", ["C:\\evil\\m.sql", "c:/evil/m.sql", "C:m.sql", "//server/share/m.sql"])
def test_manifest_rejects_windows_absolute_paths(tmp_path, path):
    # S10: drive-letter and UNC paths escape the project when joined on Windows.
    node = {
        "unique_id": "model.p.m",
        "resource_type": "model",
        "package_name": "p",
        "name": "m",
        "original_file_path": path,
    }
    payload = {
        "metadata": {"dbt_schema_version": "https://schemas.getdbt.com/dbt/manifest/v12.json", "dbt_version": "1.9.0"},
        "nodes": {"model.p.m": node},
    }
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(payload))
    with pytest.raises(ArtifactError):
        load_manifest(manifest)
