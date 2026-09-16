"""Adapter resolution, folding, and fail-closed gates."""

import pytest

from dbt_refmerge.adapters import (
    ADAPTER_SPECS,
    canonical_adapter_name,
    fold_identity,
    get_spec,
    read_profiles_target_type,
    read_project_profile,
    resolve_adapter,
)
from dbt_refmerge.domain import ReasonCode
from dbt_refmerge.errors import RefmergeError
from dbt_refmerge.source import make_identifier, parse_source_model


def test_canonical_aliases():
    assert canonical_adapter_name("PostgreSQL") == "postgres"
    assert canonical_adapter_name(" pg ") == "postgres"
    assert canonical_adapter_name("Snowflake") == "snowflake"


def test_unknown_adapter_fails_closed():
    with pytest.raises(RefmergeError) as ei:
        get_spec("mydb")
    assert ei.value.reason_code == ReasonCode.UNSUPPORTED_ADAPTER


def test_snowflake_spec_verifies_false():
    assert get_spec("snowflake").verifies is False
    assert get_spec("postgres").verifies is True


def test_fold_rules():
    assert fold_identity("Orders", False, "lower") == "orders"
    assert fold_identity("Orders", False, "upper") == "ORDERS"
    assert fold_identity("Orders", True, "upper") == "Orders"


def test_snowflake_source_folding():
    src = b"with Orders as (select x from {{ ref('m') }}) select * from ORDERS"
    model = parse_source_model(src, fold_unquoted="upper")
    assert [c.identifier.identity.value for c in model.ctes] == ["ORDERS"]
    assert make_identifier("Orders", False, "upper").identity.value == "ORDERS"


def test_bigquery_backtick_parsing():
    from dbt_refmerge.adapters import get_spec
    from dbt_refmerge.semantics import parse_model, qualify_import_cte

    assert get_spec("bigquery").verifies is False
    compiled = (
        "with my_cte as (select x, y from `proj`.ds.tbl), "
        "b as (select x from `proj`.ds.tbl) "
        "select * from my_cte join b using (x)"
    )
    parsed = parse_model(compiled, "bigquery")
    assert sorted(parsed.ctes) == ["b", "my_cte"]
    assert qualify_import_cte(parsed.ctes["my_cte"]).ok


def test_databricks_parsing():
    from dbt_refmerge.adapters import get_spec
    from dbt_refmerge.semantics import parse_model, qualify_import_cte

    assert get_spec("databricks").verifies is False
    compiled = (
        "with events as (select user_id, ts from catalog.schema.events), "
        "users as (select user_id from catalog.schema.users) "
        "select * from events join users using (user_id)"
    )
    parsed = parse_model(compiled, "databricks")
    assert sorted(parsed.ctes) == ["events", "users"]
    assert qualify_import_cte(parsed.ctes["events"]).ok


@pytest.mark.parametrize("adapter", sorted(ADAPTER_SPECS))
def test_registered_dialects_parse_generic_shape(adapter):
    from dbt_refmerge.semantics import parse_model

    spec = ADAPTER_SPECS[adapter]
    parsed = parse_model(
        "with a as (select x, y from sch.tbl), b as (select x from sch.tbl) select * from a join b using (x)",
        spec.sqlglot_dialect,
    )
    if spec.fold_unquoted == "upper":
        assert sorted(parsed.ctes) == ["A", "B"]
    else:
        assert sorted(parsed.ctes) == ["a", "b"]
    assert spec.verifies == (adapter == "postgres")


def _project(tmp_path, profile_name="myprof"):
    (tmp_path / "dbt_project.yml").write_text(f"name: p\nprofile: {profile_name}\n")
    return tmp_path


def _profiles(tmp_path, adapter_type="postgres", target="dev"):
    profiles_dir = tmp_path / "prof"
    profiles_dir.mkdir()
    (profiles_dir / "profiles.yml").write_text(
        f"myprof:\n  target: {target}\n  outputs:\n    {target}:\n      type: {adapter_type}\n      schema: s\n"
    )
    return profiles_dir


def test_resolve_from_profiles(tmp_path):
    root = _project(tmp_path)
    profiles_dir = _profiles(tmp_path)
    spec = resolve_adapter(
        cli_override=None,
        manifest_adapter=None,
        project_dir=root,
        profiles_dir=profiles_dir,
        profile=None,
        target=None,
    )
    assert spec.name == "postgres"


def test_resolve_cli_override_wins_over_profiles(tmp_path):
    root = _project(tmp_path)
    profiles_dir = _profiles(tmp_path, adapter_type="postgres")
    spec = resolve_adapter(
        cli_override="snowflake",
        manifest_adapter=None,
        project_dir=root,
        profiles_dir=profiles_dir,
        profile=None,
        target=None,
    )
    assert spec.name == "snowflake"


def test_resolve_manifest_used(tmp_path):
    root = _project(tmp_path)
    spec = resolve_adapter(
        cli_override=None,
        manifest_adapter="postgres",
        project_dir=root,
        profiles_dir=root / "missing",
        profile=None,
        target=None,
    )
    assert spec.name == "postgres"


def test_resolve_unknown_demands_adapter(tmp_path):
    root = _project(tmp_path)
    with pytest.raises(RefmergeError) as ei:
        resolve_adapter(
            cli_override=None,
            manifest_adapter=None,
            project_dir=root,
            profiles_dir=root / "missing",
            profile=None,
            target="dev",
        )
    assert ei.value.reason_code == ReasonCode.UNSUPPORTED_ADAPTER


def test_manifest_profiles_conflict_fails(tmp_path):
    root = _project(tmp_path)
    profiles_dir = _profiles(tmp_path, adapter_type="snowflake")
    with pytest.raises(RefmergeError) as ei:
        resolve_adapter(
            cli_override=None,
            manifest_adapter="postgres",
            project_dir=root,
            profiles_dir=profiles_dir,
            profile=None,
            target=None,
        )
    assert ei.value.reason_code == ReasonCode.ADAPTER_MISMATCH


def test_cli_manifest_conflict_fails(tmp_path):
    root = _project(tmp_path)
    with pytest.raises(RefmergeError) as ei:
        resolve_adapter(
            cli_override="duckdb",
            manifest_adapter="postgres",
            project_dir=root,
            profiles_dir=root / "missing",
            profile=None,
            target=None,
        )
    assert ei.value.reason_code == ReasonCode.ADAPTER_MISMATCH


def test_profiles_reader_targets(tmp_path):
    profiles_dir = _profiles(tmp_path, adapter_type="snowflake", target="prod")
    assert read_profiles_target_type(profiles_dir, "myprof", "prod") == "snowflake"
    assert read_profiles_target_type(profiles_dir, "myprof", "nope") is None
    assert read_profiles_target_type(profiles_dir, "unknown", None) is None


def test_project_profile_pointer(tmp_path):
    root = _project(tmp_path)
    assert read_project_profile(root) == "myprof"


def test_check_rejects_unverified_adapter(tmp_path):
    from dbt_refmerge.config import AppConfig
    from dbt_refmerge.orchestrator import CheckRequest, RefmergeService

    root = _project(tmp_path)
    (root / "models").mkdir()
    (root / "models" / "m.sql").write_text("select 1")
    config = AppConfig(
        project_dir=root,
        adapter="snowflake",
        scratch_schema="scratch",
        profiles_dir=tmp_path / "missing",
    )
    svc = RefmergeService()
    with pytest.raises(RefmergeError) as ei:
        svc.check(CheckRequest(config=config))
    assert ei.value.reason_code == ReasonCode.UNSUPPORTED_ADAPTER


def test_require_manifest_adapter_agreement():
    from dbt_refmerge.adapters import get_spec
    from dbt_refmerge.artifacts import ManifestMetadataModel, ManifestView
    from dbt_refmerge.orchestrator import _require_manifest_adapter

    meta = ManifestMetadataModel(
        dbt_schema_version="https://schemas.getdbt.com/dbt/manifest/v12.json",
        dbt_version="1.8.0",
        adapter_type="snowflake",
    )
    view = ManifestView(metadata=meta, nodes={}, sources={}, path=__import__("pathlib").Path("m.json"))
    _require_manifest_adapter(view, get_spec("snowflake"))  # agreement: no raise
    with pytest.raises(RefmergeError) as ei:
        _require_manifest_adapter(view, get_spec("postgres"))
    assert ei.value.reason_code == ReasonCode.ADAPTER_MISMATCH
