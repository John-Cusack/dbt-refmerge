"""Column pruning: byte preservation, binding refusals and query equivalence."""

import sqlite3
from collections import Counter
from contextlib import closing
from pathlib import Path

import pytest

from dbt_refmerge.adapters import get_spec
from dbt_refmerge.artifacts import ManifestNodeModel
from dbt_refmerge.config import AppConfig
from dbt_refmerge.domain import ReasonCode
from dbt_refmerge.errors import SemanticError
from dbt_refmerge.orchestrator import RefmergeService, ScanRequest
from dbt_refmerge.pruning import apply_expected_pruning, plan_pruning
from dbt_refmerge.reporting import render_github_scan, render_human_scan, scan_report_json
from dbt_refmerge.rewrite import apply_edits, plan_source_changes
from dbt_refmerge.semantics import parse_model
from dbt_refmerge.source import parse_source_model

REF = "{{ ref('stg') }}"


def _prune(raw, dialect="postgres"):
    source = raw.encode() if isinstance(raw, str) else raw
    model = parse_source_model(source, fold_unquoted=get_spec(dialect).fold_unquoted)
    prunings = plan_pruning(model, get_spec(dialect).sqlglot_dialect)
    return apply_edits(source, tuple(pruning.edit for pruning in prunings)), prunings


@pytest.mark.parametrize(
    ("body", "consumer", "expected"),
    [
        ("*", "id", "id"),
        ("*", "amount * 2 as total", "amount"),
        ("*", '"Account ID", id', '"Account ID", id'),
        ("id, unused, amount", "amount, id", "id, amount"),
        ("id as renamed, unused, amount", "renamed", "id as renamed"),
        ("*", "coalesce(amount, 0) as amount", "amount"),
    ],
)
def test_selects_only_named_inputs(body, consumer, expected):
    raw = f"with a as (select {body} from {REF}) select {consumer} from a\n"
    candidate, prunings = _prune(raw)
    assert len(prunings) == 1
    assert candidate.decode() == raw.replace(f"select {body} from", f"select {expected} from", 1)
    assert _prune(candidate)[0] == candidate
    assert _prune(candidate)[1] == ()


def test_traces_passthrough_stars_and_keeps_filter_inputs():
    raw = (
        f"with a as (select * from {REF} where active),\n"
        "b as (select a.* from a where account_id > 0),\n"
        "c as (select * from b where amount > 0)\n"
        "select id from c order by id\n"
    )
    candidate, prunings = _prune(raw)
    assert prunings[0].projections == ("id", "amount", "account_id")
    assert candidate.decode() == raw.replace("select * from", "select id, amount, account_id from", 1)
    # The import's own predicate needs no extra output column.
    assert "where active" in candidate.decode()


def test_keeps_inputs_in_every_clause_and_all_consumers():
    raw = (
        f"with a as (select * from {REF}),\n"
        "b as (select sum(amount) as total from a where active group by account_id having sum(amount) > 1)\n"
        "select x.id, b.total from a as x join b on x.account_id = b.total order by x.sort_key\n"
    )
    candidate, prunings = _prune(raw)
    assert set(prunings[0].projections) == {"id", "account_id", "sort_key", "amount", "active"}
    assert candidate.endswith(raw[raw.index(",\nb as") :].encode())


def test_join_using_retains_keys_on_both_imports():
    raw = (
        f"with a as (select * from {REF}), b as (select * from {{{{ source('s', 't') }}}})\n"
        "select id, a.amount from a join b using (id)\n"
    )
    candidate, prunings = _prune(raw)
    assert [(p.cte_identity, p.projections) for p in prunings] == [("a", ("id", "amount")), ("b", ("id",))]
    assert b"source('s', 't')" in candidate


def test_table_alias_and_count_star_are_supported():
    raw = f"with a as (select t.* from {REF} as t where t.active) select count(*), sum(amount) from a\n"
    candidate, prunings = _prune(raw)
    assert candidate.decode() == raw.replace("select t.*", "select amount")
    assert prunings[0].original_projections == ("t.*",)


def test_order_by_output_alias_does_not_become_an_upstream_column():
    raw = f"with a as (select * from {REF}) select amount * 2 as total from a order by total\n"
    candidate, prunings = _prune(raw)
    assert prunings[0].projections == ("amount",)
    assert candidate.decode() == raw.replace("select *", "select amount")


def test_preserves_crlf_unicode_comments_config_and_trailing_comma():
    raw = (
        "{{ config(materialized='table') }}\r\n{# keep this #}\r\n-- café\r\n"
        f"with a as (\r\n    select\r\n        id,\r\n        unused,\r\n        café,\r\n    from {REF} -- upstream\r\n)\r\n"
        "select id, café from a\r\n"
    )
    candidate, prunings = _prune(raw, "bigquery")
    assert len(prunings) == 1
    assert candidate == raw.replace("        unused,\r\n", "").encode()
    plan = plan_source_changes(
        raw.encode(), candidate, "model.p.m", Path("m.sql"), parse_source_model(raw.encode()).ctes[0].identifier
    )
    assert apply_edits(raw.encode(), plan.edits) == candidate


@pytest.mark.parametrize(
    "sql",
    [
        "select * from a",
        "select a.* from a",
        "select count(*) from a",  # no column demand: leave the import alone
        "select 1 from a",
        "select a from a",  # whole-row values
        "select count(a.*) from a",
        "select row_to_json(a) from a",
        "select id from a join other on a.id = other.id",
        "select a.id from a natural join other",
        "select a.id from a join other as a on true",
        "select a.id from a as x",  # unknown qualifier
        "select a.payload.field from a",  # struct/composite binding
        "select sum(amount) as total from a group by total",  # alias binding varies
        "select sum(amount) as total from a having total > 0",
        "select id from a union all select id from a",
        "select id from (select id from a) as x",
        "select id from a where exists (select 1 from other where other.id = a.id)",
        "select id from a; select 1",
        "select id from a, unnest(a.payload) as x",
        "select a.id from (values (1)) as x(id) join a on a.id = x.id",
        "select id from a as x(id)",
        "select id from generate_series(1, 10)",
    ],
)
def test_leaves_ambiguous_or_schema_dependent_consumers_unchanged(sql):
    raw = f"with a as (select * from {REF}) {sql}\n"
    assert _prune(raw) == (raw.encode(), ())


@pytest.mark.parametrize(
    "body",
    [
        "select distinct * from " + REF,
        "select all * from " + REF,
        "select *, amount + 1 as other from " + REF,
        "select * except (unused) from " + REF,
        "select * replace (amount + 1 as amount) from " + REF,
        "select wrong.* from " + REF,
        "select * from " + REF + " order by 1",
        "select * from " + REF + " group by all",
        "select * from " + REF + " group by id",
        "select * from " + REF + " join other using (id)",
        "select id + 1 as id, unused from " + REF,
        "select id /* keep */ as renamed, unused from " + REF,
        "select id, /* keep */ unused from " + REF,
        "select /* keep */ id, unused from " + REF,
        "select id, unused -- keep\nfrom " + REF,
        "select * -- keep\nfrom " + REF,
        "select id, {# keep #} unused from " + REF,
        "select {# keep #} * from " + REF,
        "select t.{# keep #}* from " + REF + " t",
    ],
)
def test_leaves_unsupported_imports_unchanged(body):
    raw = f"with a as ({body}) select id from a\n"
    assert _prune(raw) == (raw.encode(), ())


@pytest.mark.parametrize(
    "raw",
    [
        f"with a as (select * from {REF}) select {{{{ dynamic_column() }}}} from a",
        f"with a as (select * from {REF}) {{% if execute %}} select id from a {{% endif %}}",
        f"with a as (select * from {REF}), b as (select * from a union all select * from a) select id from b",
        f"with a as (select 1 union all select 2), b as (values (1)), c as (select * from {REF}) select id from c",
        f"with a as (select * from {REF}), b as (with x as (values (1)) select a.id from a join x on true) select id from b",
        f"with a as (select * from b), b as (select * from {REF}) select id from a",
        "select 1",
        "with a as (select * from physical) select id from a",
        f"with a as (select * from {REF}) select x.id from schema.a as x",
        f"with a as (select id, unused from {REF}) select missing from a",
        f"with a as (select id from {REF}) select id from a",
        f"with a as (select * from {REF}) select (",  # SQL parse failure
    ],
)
def test_no_edits_for_other_unsupported_shapes(raw):
    assert _prune(raw) == (raw.encode(), ())


def test_inconsistent_identifier_folding_is_not_pruned():
    raw = f"with A as (select * from {REF}) select id from A"
    model = parse_source_model(raw.encode(), fold_unquoted="none")
    assert plan_pruning(model, "postgres") == ()


@pytest.mark.parametrize(
    ("raw", "dialect"),
    [
        (f"with a as (select * from {REF}) select object_construct(*) from a", "snowflake"),
        (f"with a as (select as struct * from {REF}) select id from a", "bigquery"),
        (f"with a as (select top 10 * from {REF}) select id from a", "tsql"),
    ],
)
def test_dialect_specific_unsupported_shapes_are_not_pruned(raw, dialect):
    assert _prune(raw, dialect) == (raw.encode(), ())


def test_wildcard_trailing_comma_is_preserved():
    raw = f"with a as (select *, from {REF}) select id from a"
    candidate, prunings = _prune(raw, "bigquery")
    assert prunings
    assert candidate.decode() == raw.replace("select *,", "select id,")


@pytest.mark.parametrize(
    "consumer",
    [
        "select id, amount from a where active order by account_id",
        "select account_id, sum(amount) as total, count(*) from a group by account_id having sum(amount) > 0 order by total",
        "select x.id, y.amount from a as x left join a as y on x.account_id = y.account_id where x.active",
    ],
)
def test_pruning_preserves_schema_rows_duplicates_and_nulls(consumer):
    raw = f"with a as (select * from {REF}) {consumer}"
    candidate, prunings = _prune(raw)
    assert prunings
    with closing(sqlite3.connect(":memory:")) as conn:
        conn.execute("create table stg(id integer, account_id integer, amount numeric, active boolean, unused text)")
        conn.executemany(
            "insert into stg values (?, ?, ?, ?, ?)",
            [(1, 10, 5, 1, "x"), (1, 10, 5, 1, "x"), (2, None, None, 1, "y"), (3, 11, 2, 0, "z")],
        )
        original = conn.execute(raw.replace(REF, "stg"))
        columns, rows = original.description, Counter(original.fetchall())
        narrowed = conn.execute(candidate.decode().replace(REF, "stg"))
        assert narrowed.description == columns
        assert Counter(narrowed.fetchall()) == rows


def test_compiled_delta_checks_pruning_and_refuses_unrelated_changes():
    raw = f"with a as (select * from {REF}) select id from a\n"
    candidate, prunings = _prune(raw)
    owner = ManifestNodeModel(
        unique_id="model.p.m",
        resource_type="model",
        package_name="p",
        name="m",
        original_file_path="models/m.sql",
        raw_code=raw,
        compiled_code=raw.replace(REF, '"db"."sch"."stg"'),
    )
    candidate_node = owner.model_copy(update={"compiled_code": candidate.decode().replace(REF, '"db"."sch"."stg"')})
    service = RefmergeService()
    service._validate_delta(owner, candidate_node, (), get_spec("postgres"), prunings)
    for compiled in [
        owner.compiled_code,
        candidate_node.compiled_code + " limit 1",
        candidate_node.compiled_code.replace("select id from a", "select id + 1 from a"),
    ]:
        with pytest.raises(SemanticError) as exc_info:
            service._validate_delta(
                owner, owner.model_copy(update={"compiled_code": compiled}), (), get_spec("postgres"), prunings
            )
        assert exc_info.value.reason_code is ReasonCode.COMPILE_DRIFT


@pytest.mark.parametrize(
    "compiled",
    [
        "select 1",
        "with a as (select id from physical) select id from a",
        "with a as (select 1 union all select 2) select id from a",
    ],
)
def test_pruned_source_must_match_compiled_import_projections(compiled):
    _, prunings = _prune(f"with a as (select * from {REF}) select id from a")
    with pytest.raises(SemanticError) as exc_info:
        apply_expected_pruning(parse_model(compiled), prunings)
    assert exc_info.value.reason_code is ReasonCode.SOURCE_MAPPING_AMBIGUOUS


@pytest.mark.parametrize("adapter", ["postgres", "snowflake", "bigquery"])
def test_scan_reports_single_import_pruning_in_all_formats(make_project, adapter):
    raw = f"with prunable_orders as (\n    select * from {REF}\n)\nselect id from prunable_orders\n"
    root = make_project({"models/m.sql": raw})
    report = RefmergeService().scan(ScanRequest(AppConfig(project_dir=root, adapter=adapter)))
    assert len(report.findings) == 1
    finding = report.findings[0]
    assert (finding.cte_names, finding.reason_codes, finding.line) == (
        ("prunable_orders",),
        (ReasonCode.UNUSED_IMPORT_COLUMNS,),
        2,
    )
    human = render_human_scan(report, project_dir=root)
    assert human.startswith("models/m.sql:2: ") and human.endswith("\n")
    assert finding.cte_names[0] in human
    assert "columns" in human.lower()
    assert "PostgreSQL only" in human
    annotation = render_github_scan(report, base_dir=root)
    assert annotation.startswith("::warning ")
    properties, message = annotation[2:].split("::", 1)
    assert "file=models/m.sql,line=2," in properties
    assert properties.partition("title=")[2].strip()
    assert finding.cte_names[0] in message
    assert "columns" in message.lower()
    assert "PostgreSQL only" in message
    assert scan_report_json(report, project_dir=root)["findings"][0]["reason_codes"] == ["UNUSED_IMPORT_COLUMNS"]
