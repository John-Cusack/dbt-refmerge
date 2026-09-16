"""Harness and comparator tests (manifest loading lives in test_artifacts.py)."""

import json

import pytest

from dbt_refmerge.domain import ReasonCode
from dbt_refmerge.errors import ScratchBoundaryError, VerificationError
from dbt_refmerge.verification import comparator as comp
from dbt_refmerge.verification.harness import (
    strip_single_terminal_semicolon,
    validate_scratch_schema,
    write_harness_pair,
)


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
    with pytest.raises(Exception):
        write_harness_pair(tmp_path, "tok", "select 1 {% endraw %}", "select 1")


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
    (tmp_path / "models").mkdir()
    with pytest.raises(VerificationError) as exc_info:
        write_harness_pair(tmp_path, "tok", "select 1", candidate_sql)
    assert exc_info.value.reason_code is ReasonCode.HARNESS_EMBEDDING_UNSAFE
    assert list((tmp_path / "models").iterdir()) == []


def test_harness_pair_files_are_named_for_the_token_and_macros_clear_earlier_views(tmp_path):
    from dbt_refmerge.verification.harness import harness_node_id, write_harness_macros

    write_harness_macros(tmp_path, "p")
    spec = write_harness_pair(tmp_path, "tok", "select 1;", "select 2")

    assert (spec.baseline_alias, spec.candidate_alias, spec.baseline_sql) == (
        "dbt_refmerge_baseline_tok",
        "dbt_refmerge_candidate_tok",
        "select 1",
    )
    assert sorted(path.name for path in (tmp_path / "models").iterdir()) == ["baseline_tok.sql", "candidate_tok.sql"]
    assert harness_node_id("baseline", "tok") == "model.dbt_refmerge_harness.baseline_tok"
    write_harness_macros(tmp_path, "p")
    assert list((tmp_path / "models").iterdir()) == []


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


def test_schemas_equal_detects_a_different_column_count():
    baseline = comp.normalize_schema(
        comp.RawSchemaPayload(baseline=[{"ordinal": 1, "name": "a", "data_type": "int"}], candidate=[])
    )
    candidate = comp.RawSchemaPayload(baseline=[], candidate=[])
    assert comp.schemas_equal(baseline, candidate) is False


@pytest.mark.parametrize(
    "output",
    [
        "DBT_REFMERGE_RESULT_n_BEGIN\n{}\n{}\nDBT_REFMERGE_RESULT_n_END",
        "DBT_REFMERGE_RESULT_n_BEGIN\n" + '"' + "x" * 1_000_001 + '"' + "\nDBT_REFMERGE_RESULT_n_END",
    ],
    ids=["two-lines", "oversized"],
)
def test_parse_marked_json_refuses_bad_payload_lines(output):
    with pytest.raises(VerificationError):
        comp.parse_marked_json(output, "n")


@pytest.mark.parametrize(
    "overrides",
    [
        {"baseline_rows": True},
        {"baseline_rows": -1},
        {"baseline_rows": 2**63},
        {"baseline_rows": 1.0},
        {"baseline_rows": "1"},
        {"surprise": 1},
    ],
    ids=["bool", "negative", "too-large", "float", "string", "extra-field"],
)
def test_parse_equality_result_refuses_invalid_fields(overrides):
    with pytest.raises(VerificationError):
        comp.parse_equality_result(_payload(**overrides))


def test_parse_equality_result_requires_every_field():
    payload = _payload()
    del payload["candidate_rows"]
    with pytest.raises(VerificationError, match="missing result field"):
        comp.parse_equality_result(payload)


def test_derive_status_schema_difference_is_different():
    from dbt_refmerge.domain import EqualityResult, VerificationStatus

    assert comp.derive_status(EqualityResult(False, 1, 1, 0, 0)) == VerificationStatus.DIFFERENT


def test_semicolon_rules_refuse_two_terminated_statements():
    with pytest.raises(VerificationError):
        strip_single_terminal_semicolon("select 1; select 2;")


def test_harness_refuses_unsafe_profile_names(tmp_path):
    from dbt_refmerge.verification.harness import write_harness_macros

    with pytest.raises(VerificationError) as exc_info:
        write_harness_macros(tmp_path, 'prof"\nmodels: {}')
    assert exc_info.value.reason_code is ReasonCode.HARNESS_EMBEDDING_UNSAFE


def _harness_manifest(tmp_path, *, baseline=None, candidate=None, extra=None):
    def node(name, alias, **config):
        return {
            "unique_id": f"model.dbt_refmerge_harness.{name}",
            "resource_type": "model",
            "package_name": "dbt_refmerge_harness",
            "name": name,
            "original_file_path": f"models/{name}.sql",
            "database": "db",
            "schema": "scratch",
            "alias": alias,
            "config": {"materialized": "view", **config},
        }

    nodes = {}
    for name, overrides in (("baseline", baseline), ("candidate", candidate)):
        if overrides is not False:
            entry = node(name, f"dbt_refmerge_{name}_tok")
            entry.update(overrides or {})
            nodes[entry["unique_id"]] = entry
    nodes.update(extra or {})
    path = tmp_path / "manifest.json"
    path.write_text(
        json.dumps(
            {
                "metadata": {
                    "dbt_schema_version": "https://schemas.getdbt.com/dbt/manifest/v12.json",
                    "dbt_version": "1",
                },
                "nodes": nodes,
            }
        )
    )
    return path


def _preflight(path, **overrides):
    from dbt_refmerge.verification.harness import preflight_harness_manifest

    expected = {f"model.dbt_refmerge_harness.{role}": f"dbt_refmerge_{role}_tok" for role in ("baseline", "candidate")}
    kwargs = {"database": "db", "schema": "scratch", "expected": expected, **overrides}
    preflight_harness_manifest(path, **kwargs)


def test_preflight_accepts_the_expected_views_and_disabled_or_test_nodes(tmp_path):
    extra = {
        "model.dbt_refmerge_harness.off": {
            "unique_id": "model.dbt_refmerge_harness.off",
            "resource_type": "model",
            "package_name": "dbt_refmerge_harness",
            "name": "off",
            "original_file_path": "models/off.sql",
            "config": {"enabled": False},
        },
        "test.dbt_refmerge_harness.t": {
            "unique_id": "test.dbt_refmerge_harness.t",
            "resource_type": "test",
            "package_name": "dbt_refmerge_harness",
            "name": "t",
            "original_file_path": "tests/t.sql",
        },
    }
    _preflight(_harness_manifest(tmp_path, extra=extra))


@pytest.mark.parametrize(
    "kwargs",
    [
        {"candidate": False},
        {"baseline": {"config": {"materialized": "view", "post-hook": ["drop table x"]}}},
        {"baseline": {"config": {"materialized": "table"}}},
        {"baseline": {"alias": "customers"}},
        {"baseline": {"database": "other"}},
        {"baseline": {"schema": "public"}},
        {"extra": {"model.dbt_refmerge_harness.other": {"resource_type": "model", "config": {}}}},
    ],
    ids=["missing-candidate", "hook", "table", "alias-not-expected", "other-database", "other-schema", "extra-model"],
)
def test_preflight_refuses(tmp_path, kwargs):
    extra = kwargs.pop("extra", None)
    if extra:
        name = "other"
        extra = {
            uid: {
                "unique_id": uid,
                "package_name": "dbt_refmerge_harness",
                "name": name,
                "original_file_path": f"models/{name}.sql",
                **node,
            }
            for uid, node in extra.items()
        }
    with pytest.raises(ScratchBoundaryError):
        _preflight(_harness_manifest(tmp_path, extra=extra, **kwargs))


def test_preflight_refuses_an_empty_batch(tmp_path):
    with pytest.raises(ScratchBoundaryError):
        _preflight(_harness_manifest(tmp_path), expected={})


def test_build_verdict_sql_dispatches_by_strategy():
    from dbt_refmerge.verification.harness import build_verdict_sql

    relations = ('"db"."s"."b"', '"db"."s"."c"', ["x"])
    assert build_verdict_sql(*relations, "grouped_counts") == comp.generate_grouped_counts_sql(*relations)
    assert build_verdict_sql(*relations, "except_all") == comp.generate_except_all_sql(*relations)
