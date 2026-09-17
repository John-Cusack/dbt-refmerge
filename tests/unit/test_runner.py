"""Warehouse verifier helpers that need no dbt: SQL literals, result parsing, profile lookup."""

import pytest

from dbt_refmerge.config import AppConfig
from dbt_refmerge.domain import ReasonCode
from dbt_refmerge.errors import ScratchBoundaryError, VerificationError
from dbt_refmerge.verification.runner import (
    HarnessSession,
    QueryResult,
    _counts,
    _failed_nodes,
    _profile_name,
    _query_result,
    _schema_payload,
    catalog_sql,
    sql_literal,
    verify_postgres_batch,
)

COUNT_COLUMNS = ("baseline_rows", "candidate_rows", "baseline_only_occurrences", "candidate_only_occurrences")


@pytest.mark.parametrize("value", ["a\\b", "a\x00b"], ids=["backslash", "nul"])
def test_sql_literal_refuses_setting_dependent_characters(value):
    with pytest.raises(VerificationError) as exc_info:
        sql_literal(value)
    assert exc_info.value.reason_code is ReasonCode.HARNESS_EMBEDDING_UNSAFE


def test_sql_literal_doubles_quotes():
    assert sql_literal("it's") == "'it''s'"
    assert "c.relname in ('a', 'b''c')" in catalog_sql("s", ["a", "b'c"])


def _catalog(*rows):
    return QueryResult(columns=("relname", "relkind", "attnum", "attname", "format_type"), rows=tuple(rows))


def test_schema_payload_orders_columns_per_relation():
    payload = _schema_payload(
        _catalog(("b", "v", "1", "x", "integer"), ("c", "v", "1", "x", "integer"), ("b", "v", "2", "y", "text")),
        "b",
        "c",
    )
    assert [e["name"] for e in payload.baseline] == ["x", "y"] and [e["name"] for e in payload.candidate] == ["x"]


@pytest.mark.parametrize(
    ("rows", "error"),
    [
        ((("other", "v", "1", "x", "integer"),), VerificationError),
        ((("b", "v", "one", "x", "integer"), ("c", "v", "1", "x", "integer")), VerificationError),
        ((("b", "v", "1", None, "integer"), ("c", "v", "1", "x", "integer")), VerificationError),
        ((("b", "r", "1", "x", "integer"), ("c", "v", "1", "x", "integer")), ScratchBoundaryError),
        ((("b", "v", None, None, None), ("c", "v", "1", "x", "integer")), ScratchBoundaryError),
        ((("b", "v", "1", "x", "integer"),), ScratchBoundaryError),
    ],
    ids=[
        "unexpected-relation",
        "non-numeric-ordinal",
        "null-name",
        "table-not-view",
        "no-columns",
        "missing-candidate",
    ],
)
def test_schema_payload_refuses_unexpected_catalog_rows(rows, error):
    with pytest.raises(error):
        _schema_payload(_catalog(*rows), "b", "c")


@pytest.mark.parametrize(
    "result",
    [
        QueryResult(columns=("baseline_rows",), rows=(("1",),)),
        QueryResult(columns=COUNT_COLUMNS, rows=()),
        QueryResult(columns=COUNT_COLUMNS, rows=(("1", "1", "0", "-1"),)),
        QueryResult(columns=COUNT_COLUMNS, rows=(("1", "1", None, "0"),)),
        QueryResult(columns=COUNT_COLUMNS, rows=(("1", "1", "0", "1" * 20),)),
    ],
    ids=["wrong-columns", "no-row", "negative", "null", "too-large"],
)
def test_counts_refuse_malformed_verdicts(result):
    with pytest.raises(VerificationError):
        _counts(result)


@pytest.mark.parametrize(
    "payload",
    [
        {"columns": ["a"], "rows": [], "extra": 1},
        {"columns": "a", "rows": []},
        {"columns": [1], "rows": []},
        {"columns": ["a"], "rows": [["x", "y"]]},
        {"columns": ["a"], "rows": ["x"]},
    ],
    ids=["extra-key", "columns-not-list", "non-string-column", "row-too-wide", "row-not-list"],
)
def test_query_result_refuses_malformed_payloads(payload):
    with pytest.raises(VerificationError):
        _query_result(payload)


def test_profile_name_comes_from_config_or_project(make_project):
    root = make_project({}, profile="from_project")
    assert _profile_name(AppConfig(project_dir=root), root) == "from_project"
    assert _profile_name(AppConfig(project_dir=root, profile="explicit"), root) == "explicit"

    (root / "dbt_project.yml").write_text("name: p\n")
    with pytest.raises(VerificationError, match="no dbt profile"):
        _profile_name(AppConfig(project_dir=root), root)


def test_harness_uses_the_projects_profiles_yml_when_no_profiles_dir_is_given(make_project, tmp_path):
    from dbt_refmerge.dbt_cli import DbtCli

    root = make_project({})
    without = HarnessSession(DbtCli(("dbt",)), tmp_path, AppConfig(project_dir=root), "s", tmp_path / "t")
    (root / "profiles.yml").write_text("p: {}\n")
    with_file = HarnessSession(DbtCli(("dbt",)), tmp_path, AppConfig(project_dir=root), "s", tmp_path / "t")

    assert (without.invocation.profiles_dir, with_file.invocation.profiles_dir) == (None, root)


def test_an_empty_batch_needs_no_warehouse():
    assert verify_postgres_batch([]) == []


@pytest.mark.parametrize(
    ("content", "failed"),
    [
        (
            '{"results": [{"unique_id": "a", "status": "success"}, "junk", {"unique_id": "b", "status": "error"}]}',
            {"b"},
        ),
        ("{not json", set()),
        ('{"no_results": []}', set()),
        ('{"results": [{"status": "error"}]}', set()),
        ('{"results": 1}', set()),
    ],
    ids=["mixed", "bad-json", "no-results-key", "no-unique-id", "results-not-a-list"],
)
def test_failed_nodes_reads_run_results_and_attributes_nothing_it_cannot_read(tmp_path, content, failed):
    path = tmp_path / "run_results.json"
    path.write_text(content)

    assert _failed_nodes(path) == failed
    assert _failed_nodes(tmp_path / "missing.json") == set()
