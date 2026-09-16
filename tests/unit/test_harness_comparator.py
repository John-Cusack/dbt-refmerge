"""Artifacts, harness, comparator tests."""

import json
from pathlib import Path

import pytest

from dbt_refmerge.artifacts import load_manifest
from dbt_refmerge.domain import ReasonCode
from dbt_refmerge.errors import ArtifactError
from dbt_refmerge.verification import comparator as comp
from dbt_refmerge.verification.harness import build_harness_project, strip_single_terminal_semicolon
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
    sql = comp.generate_except_all_sql("s.b", "s.c", ["a", "b"])
    assert "except all" in sql.lower()
    grouped = comp.generate_grouped_counts_sql("s.b", "s.c", ["a"])
    assert "union all" in grouped.lower()


def test_harness_rejects_endraw(tmp_path):
    ws = RunWorkspace("20260101T000000_abcdef123456", tmp_path)
    with pytest.raises(Exception):
        build_harness_project(ws, profile="p", baseline_sql="select 1 {% endraw %}", candidate_sql="select 1")


def test_semicolon_rules():
    assert strip_single_terminal_semicolon("select 1;") == "select 1"
    with pytest.raises(Exception):
        strip_single_terminal_semicolon("select 1; select 2")
