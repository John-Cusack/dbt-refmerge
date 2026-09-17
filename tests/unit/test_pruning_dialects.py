"""Dialect-specific pruning from SQL text, without dbt or a warehouse."""

import sqlite3
from collections import Counter
from contextlib import closing

import pytest
from sqlglot import parse_one
from sqlglot.optimizer.qualify import qualify

from dbt_refmerge.adapters import ADAPTER_SPECS, get_spec
from dbt_refmerge.artifacts import ManifestNodeModel
from dbt_refmerge.config import AppConfig
from dbt_refmerge.domain import ReasonCode
from dbt_refmerge.orchestrator import RefmergeService, ScanRequest
from dbt_refmerge.pruning import plan_pruning
from dbt_refmerge.rewrite import apply_edits
from dbt_refmerge.source import parse_source_model

REF = "{{ ref('stg') }}"


def _prune(raw, dialect):
    spec = get_spec(dialect)
    source = parse_source_model(raw.encode(), fold_unquoted=spec.fold_unquoted)
    prunings = plan_pruning(source, spec.sqlglot_dialect)
    candidate = apply_edits(raw.encode(), tuple(pruning.edit for pruning in prunings)).decode()
    return candidate, prunings


@pytest.mark.parametrize("dialect", ADAPTER_SPECS)
def test_basic_pruning_works_in_every_configured_dialect(dialect):
    raw = f"with orders as (select * from {REF}) select id, amount * 2 as total from orders where active"
    candidate, prunings = _prune(raw, dialect)
    assert prunings[0].projections == ("id", "active", "amount")
    assert candidate == raw.replace("select *", "select id, active, amount")
    assert _prune(candidate, dialect) == (candidate, ())


@pytest.mark.parametrize(
    ("body", "consumer", "expected"),
    [
        ("*", "`Order ID`", "`Order ID`"),
        ("`Order ID` as `ID`, unused", "`ID`", "`Order ID` as `ID`"),
        ("`Order ID`, unused, amount", "amount, `Order ID`", "`Order ID`, amount"),
        ("*", "`group`", "`group`"),
    ],
)
def test_bigquery_backtick_ctes_columns_and_aliases_preserve_source(body, consumer, expected):
    raw = f"with `Order Imports` as (select {body} from {REF}) select {consumer} from `Order Imports`\n"
    candidate, prunings = _prune(raw, "bigquery")
    assert prunings[0].cte_identity == "Order Imports"
    assert candidate == raw.replace(f"select {body} from", f"select {expected} from", 1)
    assert _prune(candidate, "bigquery") == (candidate, ())


def test_bigquery_backticks_are_bound_through_passthrough_and_join_aliases():
    raw = (
        f"with `orders` as (select * from {REF}),\n"
        "`filtered orders` as (select `orders`.* from `orders` where `Active Flag`)\n"
        "select `left`.id from `filtered orders` as `left` join `orders` as `right`\n"
        "on `left`.`Account ID` = `right`.`Account ID`\n"
    )
    candidate, prunings = _prune(raw, "bigquery")
    assert set(prunings[0].projections) == {"id", "`Account ID`", "`Active Flag`"}
    assert candidate[candidate.index(",\n") :] == raw[raw.index(",\n") :]


@pytest.mark.parametrize(
    ("body", "consumer", "expected"),
    [
        ("id, amount, unused", "`ID`, amount", "id, amount"),
        ("`ID`, amount, unused", "id, amount", "`ID`, amount"),
        ("id as `OrderID`, amount, unused", "orderid, amount", "id as `OrderID`, amount"),
        ("*", "id, `ID`", "id"),
    ],
)
def test_bigquery_quoted_columns_and_query_aliases_bind_case_insensitively(body, consumer, expected):
    raw = f"with `Orders` as (select {body} from {REF}) select {consumer} from `orders`"
    candidate, prunings = _prune(raw, "bigquery")
    assert prunings[0].cte_identity == "Orders"
    assert candidate == raw.replace(f"select {body} from", f"select {expected} from", 1)


def test_bigquery_case_variants_do_not_bypass_wildcard_exclusions():
    raw = f"with orders as (select * except (`ID`) from {REF}) select id, amount from orders"
    assert _prune(raw, "bigquery") == (raw, ())


def test_bigquery_duplicate_ctes_that_differ_only_in_case_are_not_pruned():
    raw = f"with `Orders` as (select * from {REF}), `orders` as (select * from {REF}) select id from orders"
    assert _prune(raw, "bigquery") == (raw, ())


@pytest.mark.parametrize(
    "raw",
    [
        f"with orders as (select `\\u0049D`, amount, unused from {REF}) select id, amount from orders",
        f"with orders as (select * from {REF}) select `o.payload`.field from orders as o",
        f"with orders as (select * from {REF}) select `o.payload.field` from orders as o",
    ],
)
def test_bigquery_escaped_and_dotted_backtick_tokens_are_not_pruned(raw):
    assert _prune(raw, "bigquery") == (raw, ())


@pytest.mark.parametrize(
    ("dialect", "body", "expected"),
    [
        ("bigquery", "* except (unused, note)", "id, amount"),
        ("bigquery", "t.* except (`Unused Column`)", "id, amount"),
        ("snowflake", "* exclude unused", "id, amount"),
        ("snowflake", '* exclude (unused, "Unused Column")', "id, amount"),
        ("snowflake", "t.* exclude (unused, note)", "id, amount"),
    ],
)
def test_excluded_wildcards_can_be_narrowed_without_requiring_excluded_columns(dialect, body, expected):
    alias = " as t" if body.startswith("t.") else ""
    raw = f"with orders as (select {body} from {REF}{alias}) select id, amount from orders"
    candidate, prunings = _prune(raw, dialect)
    assert prunings[0].projections == ("id", "amount")
    assert candidate == raw.replace(f"select {body}", f"select {expected}", 1)


@pytest.mark.parametrize(("dialect", "modifier"), [("bigquery", "except"), ("snowflake", "exclude")])
def test_exclusions_in_passthrough_ctes_are_traced_with_filter_inputs(dialect, modifier):
    raw = (
        f"with orders as (select * from {REF}),\n"
        f"filtered as (select * {modifier} (unused) from orders where active)\n"
        "select id from filtered"
    )
    candidate, prunings = _prune(raw, dialect)
    assert set(prunings[0].projections) == {"id", "active", "unused"}
    assert candidate[candidate.index(",\n") :] == raw[raw.index(",\n") :]


@pytest.mark.parametrize(("dialect", "modifier"), [("bigquery", "except"), ("snowflake", "exclude")])
@pytest.mark.parametrize("passthrough", [False, True])
def test_excluded_wildcard_candidates_preserve_rows_nulls_duplicates_and_output_columns(dialect, modifier, passthrough):
    body = (
        f"orders as (select * from {REF}), filtered as (select * {modifier} (unused) from orders where active)"
        if passthrough
        else f"filtered as (select * {modifier} (unused) from {REF} where active)"
    )
    raw = f"with {body} select id, amount from filtered"
    candidate, prunings = _prune(raw, dialect)
    assert prunings
    schema = {"stg": {"id": "INT", "amount": "INT", "active": "BOOLEAN", "unused": "TEXT", "note": "TEXT"}}

    def sqlite_sql(sql):
        # Expand each wildcard independently using this fixture's known schema.
        # Only the test oracle uses a schema; production analysis reads text alone.
        return qualify(parse_one(sql.replace(REF, "stg"), read=dialect), dialect=dialect, schema=schema).sql(
            dialect="sqlite"
        )

    with closing(sqlite3.connect(":memory:")) as conn:
        conn.execute("create table stg(id integer, amount integer, active boolean, unused text, note text)")
        conn.executemany(
            "insert into stg values (?, ?, ?, ?, ?)",
            [(1, 5, 1, "x", "n"), (1, 5, 1, "x", "n"), (2, None, 1, "y", "n"), (3, 2, 0, "z", "n")],
        )
        original = conn.execute(sqlite_sql(raw))
        columns, rows = original.description, Counter(original.fetchall())
        narrowed = conn.execute(sqlite_sql(candidate))
        assert narrowed.description == columns
        assert Counter(narrowed.fetchall()) == rows


@pytest.mark.parametrize(
    ("dialect", "body", "consumer"),
    [
        ("bigquery", "* except (id)", "id"),
        ("snowflake", "* exclude id", "id"),
        ("bigquery", "* replace (amount * 2 as amount)", "amount"),
        ("snowflake", "* rename id as order_id", "order_id"),
        ("snowflake", "* ilike '%id%'", "id"),
        ("bigquery", "* except (o.id)", "id"),
        ("bigquery", "* except (*)", "id"),
        ("snowflake", '* exclude "Id"', '"Id"'),
    ],
)
def test_excluded_demands_and_other_wildcard_modifiers_remain_unsupported(dialect, body, consumer):
    raw = f"with orders as (select {body} from {REF}) select {consumer} from orders"
    assert _prune(raw, dialect) == (raw, ())


@pytest.mark.parametrize(
    "consumer",
    ["select * except (unused) from orders", "select orders.* except (unused) from orders"],
)
def test_bigquery_final_wildcards_still_need_a_schema(consumer):
    raw = f"with orders as (select * from {REF}) {consumer}"
    assert _prune(raw, "bigquery") == (raw, ())


@pytest.mark.parametrize(
    ("consumer", "expected"),
    [
        ("o.payload.field", "payload"),
        ("o.payload.nested.field", "payload"),
        ("o.`Payload Data`.`Field Name`", "`Payload Data`"),
        ("o.items[offset(0)].value", "items"),
    ],
)
def test_bigquery_qualified_nested_fields_keep_the_entire_input_column(consumer, expected):
    raw = f"with orders as (select * from {REF}) select {consumer} from orders as o"
    candidate, prunings = _prune(raw, "bigquery")
    assert prunings[0].projections == (expected,)
    assert candidate == raw.replace("select *", f"select {expected}", 1)


@pytest.mark.parametrize("consumer", ["payload.field", "unknown.payload.field", "o.payload.*", "o"])
def test_bigquery_unbound_nested_fields_and_whole_rows_are_not_pruned(consumer):
    raw = f"with orders as (select * from {REF}) select {consumer} from orders as o"
    assert _prune(raw, "bigquery") == (raw, ())


def test_snowflake_variant_paths_keep_the_input_column_and_quoted_names():
    raw = (
        f'with "Order Imports" as (select * from {REF}) '
        'select o.payload:customer.id::string as customer_id, o."Order ID" from "Order Imports" as o'
    )
    candidate, prunings = _prune(raw, "snowflake")
    assert set(prunings[0].projections) == {"payload", '"Order ID"'}
    assert candidate[candidate.index(") select") :] == raw[raw.index(") select") :]


@pytest.mark.parametrize(
    ("dialect", "consumer", "expected"),
    [
        (
            "bigquery",
            "select o.id, item.value from orders o cross join unnest(o.items) as item",
            ("id", "items"),
        ),
        (
            "bigquery",
            "select o.id, item.value from orders o left join unnest(o.items) as item on item.active",
            ("id", "items"),
        ),
        (
            "bigquery",
            "select o.id, item.nested.value from orders o cross join unnest(o.items) as item where item.active",
            ("id", "items"),
        ),
        (
            "bigquery",
            "select o.id, sub.value from orders o cross join unnest(o.items) as item cross join unnest(item.children) as sub",
            ("id", "items"),
        ),
        (
            "snowflake",
            "select o.id, f.value from orders o, lateral flatten(input => o.payload) as f",
            ("id", "payload"),
        ),
        (
            "snowflake",
            "select o.id from orders o, lateral flatten(input => o.payload)",
            ("id", "payload"),
        ),
        (
            "snowflake",
            "select o.id, f.value:customer::string from orders o, lateral flatten(input => o.payload, outer => true) as f",
            ("id", "payload"),
        ),
        (
            "snowflake",
            "select o.id, child.value from orders o, lateral flatten(input => o.payload) as f, lateral flatten(input => f.value:children) as child",
            ("id", "payload"),
        ),
    ],
)
def test_qualified_unnest_and_flatten_keep_the_complete_array_or_payload(dialect, consumer, expected):
    raw = f"with orders as (select * from {REF}) {consumer}"
    candidate, prunings = _prune(raw, dialect)
    assert prunings[0].projections == expected
    assert candidate == raw.replace("select *", "select " + ", ".join(expected), 1)


@pytest.mark.parametrize(
    ("dialect", "consumer"),
    [
        ("bigquery", "select id, item.value from orders o cross join unnest(o.items) as item"),
        ("bigquery", "select o.id, item.value from orders o cross join unnest(items) as item"),
        ("bigquery", "select o.id from orders o cross join unnest(o.items)"),
        ("bigquery", "select o.id from orders o cross join unnest(o.items) as o"),
        ("bigquery", "select o.id, item.value from orders o cross join unnest(o.items) as item with offset as pos"),
        ("bigquery", "select o.id from orders o cross join unnest(o.items, o.other) as item"),
        ("bigquery", "select o.id, item.value from orders o, o.items as item"),
        ("bigquery", "select o.id, item.value from orders o, `o.items` as item"),
        ("snowflake", "select o.id, value from orders o, lateral flatten(input => o.payload) as f"),
        ("snowflake", "select o.id, f.value from orders o, lateral flatten(input => payload) as f"),
        ("snowflake", "select o.id from orders o, lateral flatten(input => o.payload) as o"),
        ("snowflake", "select o.id from orders o, lateral custom_function(o.payload) as f"),
        ("snowflake", "select o.id from orders o, table(custom_function(o.payload)) as f"),
    ],
)
def test_ambiguous_and_other_table_function_forms_are_not_pruned(dialect, consumer):
    raw = f"with orders as (select * from {REF}) {consumer}"
    assert _prune(raw, dialect) == (raw, ())


@pytest.mark.parametrize("dialect", ["snowflake", "bigquery"])
@pytest.mark.parametrize(
    "consumer",
    [
        "select id from orders pivot (sum(amount) for status in ('paid', 'void'))",
        "select id, status, amount from orders unpivot (amount for status in (paid, void))",
    ],
)
def test_pivot_and_unpivot_need_the_full_input_shape(dialect, consumer):
    raw = f"with orders as (select * from {REF}) {consumer}"
    assert _prune(raw, dialect) == (raw, ())


@pytest.mark.parametrize("dialect", ["snowflake", "bigquery"])
def test_qualify_window_inputs_are_retained_without_alias_binding(dialect):
    raw = (
        f"with orders as (select * from {REF}) select id from orders "
        "qualify row_number() over (partition by account_id order by created_at) = 1"
    )
    candidate, prunings = _prune(raw, dialect)
    assert set(prunings[0].projections) == {"id", "account_id", "created_at"}
    assert candidate[candidate.index(") select") :] == raw[raw.index(") select") :]


@pytest.mark.parametrize("dialect", ["snowflake", "bigquery"])
def test_qualify_output_alias_binding_still_requires_schema_information(dialect):
    raw = (
        f"with orders as (select * from {REF}) "
        "select id, row_number() over (order by created_at) as rn from orders qualify rn = 1"
    )
    assert _prune(raw, dialect) == (raw, ())


@pytest.mark.parametrize(
    "consumer",
    [
        "select amount * 2 as total, total + 1 as grand_total from orders",
        "select amount * 2 as total from orders where total > 0",
        "select amount * 2 as total from orders join other on total = other.id",
    ],
)
def test_snowflake_reused_output_aliases_are_not_guessed_as_input_columns(consumer):
    raw = f"with orders as (select * from {REF}) {consumer}"
    assert _prune(raw, "snowflake") == (raw, ())


def test_snowflake_self_named_projection_alias_and_top_level_order_by_are_supported():
    raw = f"with orders as (select * from {REF}) select coalesce(amount, 0) as amount from orders order by amount"
    candidate, prunings = _prune(raw, "snowflake")
    assert prunings[0].projections == ("amount",)
    assert candidate == raw.replace("select *", "select amount", 1)


@pytest.mark.parametrize(
    ("dialect", "body", "consumer", "relation"),
    [
        ("snowflake", "* exclude unused", "o.payload:field::string", '"DB"."SCH"."STG"'),
        ("bigquery", "* except (unused)", "o.payload.field", "`project.dataset.stg`"),
        ("bigquery", "`Order ID`, unused", "o.`Order ID`", "`project.dataset.stg`"),
    ],
)
def test_dialect_pruning_passes_the_compiled_delta_gate_from_text(dialect, body, consumer, relation):
    raw = f"with orders as (select {body} from {REF}) select {consumer} from orders as o"
    candidate, prunings = _prune(raw, dialect)
    assert prunings
    owner = ManifestNodeModel(
        unique_id="model.p.m",
        resource_type="model",
        package_name="p",
        name="m",
        original_file_path="models/m.sql",
        raw_code=raw,
        compiled_code=raw.replace(REF, relation),
    )
    candidate_node = owner.model_copy(update={"compiled_code": candidate.replace(REF, relation)})
    RefmergeService()._validate_delta(owner, candidate_node, (), get_spec(dialect), prunings)


@pytest.mark.parametrize(
    ("dialect", "consumer", "relation"),
    [
        (
            "snowflake",
            "select o.id, f.value from orders o, lateral flatten(input => o.payload) as f",
            '"DB"."SCH"."STG"',
        ),
        (
            "bigquery",
            "select o.id, item.value from orders o cross join unnest(o.items) as item",
            "`project.dataset.stg`",
        ),
    ],
)
def test_row_expansion_is_unchanged_in_the_compiled_delta(dialect, consumer, relation):
    raw = f"with orders as (select * from {REF}) {consumer}"
    candidate, prunings = _prune(raw, dialect)
    assert prunings
    owner = ManifestNodeModel(
        unique_id="model.p.m",
        resource_type="model",
        package_name="p",
        name="m",
        original_file_path="models/m.sql",
        compiled_code=raw.replace(REF, relation),
    )
    candidate_node = owner.model_copy(update={"compiled_code": candidate.replace(REF, relation)})
    RefmergeService()._validate_delta(owner, candidate_node, (), get_spec(dialect), prunings)


@pytest.mark.parametrize(("dialect", "body"), [("snowflake", "* exclude unused"), ("bigquery", "* except (unused)")])
def test_scan_detects_dialect_pruning_without_dbt_profiles_or_connections(make_project, dialect, body):
    raw = f"with orders as (select {body} from {REF}) select id from orders"
    root = make_project({"models/m.sql": raw})

    def unexpected_call(*_args, **_kwargs):
        pytest.fail("source scanning must not invoke dbt or warehouse verification")

    service = RefmergeService(dbt_factory=unexpected_call, verify_runner=unexpected_call)
    report = service.scan(ScanRequest(AppConfig(project_dir=root, adapter=dialect)))
    assert len(report.findings) == 1
    assert report.findings[0].reason_codes == (ReasonCode.UNUSED_IMPORT_COLUMNS,)
    assert (root / "models/m.sql").read_text() == raw
