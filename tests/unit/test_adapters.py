"""Adapter resolution, folding, and fail-closed gates."""

from pathlib import Path

import pytest

from dbt_refmerge.adapters import (
    ADAPTER_SPECS,
    MAX_PROFILES_BYTES,
    canonical_adapter_name,
    fold_identity,
    get_spec,
    read_profiles_target_type,
    read_project_profile,
    resolve_adapter,
    spec_for_dialect,
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


@pytest.mark.parametrize("raw", ["", "   ", "x" * 65, "post\x00gres"], ids=["empty", "blank", "65-chars", "nul"])
def test_canonical_adapter_name_rejects(raw):
    with pytest.raises(RefmergeError) as ei:
        canonical_adapter_name(raw)
    assert ei.value.reason_code == ReasonCode.UNSUPPORTED_ADAPTER


def test_spec_for_dialect_round_trips_and_unknown_fails_closed():
    assert spec_for_dialect("postgres") is ADAPTER_SPECS["postgres"]
    assert spec_for_dialect("tsql") is ADAPTER_SPECS["tsql"]
    with pytest.raises(RefmergeError) as ei:
        spec_for_dialect("mysql")
    assert ei.value.reason_code == ReasonCode.UNSUPPORTED_ADAPTER


# -- dbt_project.yml profile pointer ----------------------------------------------------------------


def _sparse(path, size):
    with path.open("wb") as fh:
        fh.truncate(size)


def test_project_profile_yaml_extension(tmp_path):
    (tmp_path / "dbt_project.yaml").write_text("name: p\nprofile: '  yamlprof  '\n")
    assert read_project_profile(tmp_path) == "yamlprof"


def test_project_profile_yml_wins_even_without_profile(tmp_path):
    (tmp_path / "dbt_project.yml").write_text("name: p\n")
    (tmp_path / "dbt_project.yaml").write_text("name: p\nprofile: yamlprof\n")
    assert read_project_profile(tmp_path) is None


def test_project_profile_absent_returns_none(tmp_path):
    assert read_project_profile(tmp_path) is None


@pytest.mark.parametrize(
    "content",
    [
        b"name: p\n",
        b"profile: '   '\n",
        b"profile: 7\n",
        b"- profile: p\n",
        b"profile: [unclosed\n",
        b"profile: \xff\n",
    ],
    ids=["no-profile", "blank-profile", "non-string-profile", "list-root", "invalid-yaml", "non-utf8"],
)
def test_project_profile_unusable_returns_none(tmp_path, content):
    (tmp_path / "dbt_project.yml").write_bytes(content)
    assert read_project_profile(tmp_path) is None


def test_project_profile_oversized_returns_none(tmp_path):
    _sparse(tmp_path / "dbt_project.yml", MAX_PROFILES_BYTES + 1)
    assert read_project_profile(tmp_path) is None


# -- profiles.yml target type -----------------------------------------------------------------------


def _profiles_text(tmp_path, text, filename="profiles.yml"):
    profiles_dir = tmp_path / "prof"
    profiles_dir.mkdir(exist_ok=True)
    (profiles_dir / filename).write_text(text)
    return profiles_dir


def test_profiles_reader_missing_target_key_uses_default_target(tmp_path):
    # dbt falls back to the target named "default", not "dev".
    profiles_dir = _profiles_text(
        tmp_path, "myprof:\n  outputs:\n    dev:\n      type: snowflake\n    default:\n      type: postgres\n"
    )
    assert read_profiles_target_type(profiles_dir, "myprof", None) == "postgres"


@pytest.mark.parametrize(
    "target_line",
    ["  target:\n", "  target: ''\n", "  target: \"{{ env_var('T') }}\"\n"],
    ids=["null", "empty", "unrendered-jinja"],
)
def test_profiles_reader_unusable_target_key_returns_none(tmp_path, target_line):
    # dbt uses the key whenever it is present (rendering Jinja); an unusable value is a dbt error, not "default".
    profiles_dir = _profiles_text(
        tmp_path,
        "myprof:\n" + target_line + "  outputs:\n    dev:\n      type: snowflake\n    default:\n      type: postgres\n",
    )
    assert read_profiles_target_type(profiles_dir, "myprof", None) is None


def test_profiles_reader_target_override_wins_and_empty_override_is_ignored(tmp_path):
    # The tool never passes an empty --target to dbt, so "" means "use the profile's target".
    profiles_dir = _profiles_text(
        tmp_path,
        "myprof:\n  target: dev\n  outputs:\n    dev:\n      type: snowflake\n    prod:\n      type: postgres\n",
    )
    assert read_profiles_target_type(profiles_dir, "myprof", "prod") == "postgres"
    assert read_profiles_target_type(profiles_dir, "myprof", "") == "snowflake"


def test_profiles_reader_reads_only_profiles_yml(tmp_path):
    # dbt-core reads <profiles dir>/profiles.yml and nothing else.
    profiles_dir = _profiles_text(
        tmp_path, "myprof:\n  target: dev\n  outputs:\n    dev:\n      type: postgres\n", "profiles.yaml"
    )
    assert read_profiles_target_type(profiles_dir, "myprof", None) is None
    assert read_profiles_target_type(profiles_dir, "myprof", "dev") is None


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"myprof: [unclosed\n",
        b"myprof: \xff\n",
        b"- myprof\n",
        b"other:\n  outputs:\n    dev:\n      type: postgres\n",
        b"myprof: postgres\n",
        b"myprof:\n  target: dev\n",
        b"myprof:\n  target: dev\n  outputs: [dev]\n",
        b"myprof:\n  target: dev\n  outputs: {}\n",
        b"myprof:\n  target: dev\n  outputs:\n    dev: postgres\n",
        b"myprof:\n  target: dev\n  outputs:\n    dev:\n      schema: s\n",
        b"myprof:\n  target: dev\n  outputs:\n    dev:\n      type: '  '\n",
        b"myprof:\n  target: dev\n  outputs:\n    dev:\n      type: 5\n",
    ],
    ids=[
        "empty",
        "invalid-yaml",
        "non-utf8",
        "list-root",
        "profile-absent",
        "profile-not-mapping",
        "outputs-absent",
        "outputs-list",
        "outputs-empty",
        "output-not-mapping",
        "type-absent",
        "type-blank",
        "type-non-string",
    ],
)
def test_profiles_reader_unusable_returns_none(tmp_path, content):
    profiles_dir = tmp_path / "prof"
    profiles_dir.mkdir()
    (profiles_dir / "profiles.yml").write_bytes(content)
    assert read_profiles_target_type(profiles_dir, "myprof", None) is None


def test_profiles_reader_missing_or_oversized_returns_none(tmp_path):
    assert read_profiles_target_type(tmp_path / "absent", "myprof", None) is None
    _sparse(tmp_path / "profiles.yml", MAX_PROFILES_BYTES + 1)
    assert read_profiles_target_type(tmp_path, "myprof", None) is None


def test_profiles_reader_strips_type(tmp_path):
    profiles_dir = _profiles_text(tmp_path, "myprof:\n  target: dev\n  outputs:\n    dev:\n      type: ' Postgres '\n")
    assert read_profiles_target_type(profiles_dir, "myprof", None) == "Postgres"


# -- profiles directory precedence (dbt: --profiles-dir > DBT_PROFILES_DIR > ./profiles.yml > ~/.dbt) --


def _write_profiles(directory: Path, adapter_type: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "profiles.yml").write_text(
        f"myprof:\n  target: dev\n  outputs:\n    dev:\n      type: {adapter_type}\n"
    )
    return directory


def _resolve(project_dir, profiles_dir=None):
    return resolve_adapter(
        cli_override=None,
        manifest_adapter=None,
        project_dir=project_dir,
        profiles_dir=profiles_dir,
        profile=None,
        target=None,
    ).name


def _project_at(path):
    path.mkdir(parents=True)
    return _project(path)


def test_resolve_adapter_profiles_dir_precedence_matches_dbt(tmp_path, monkeypatch):
    root = _project_at(tmp_path / "project")
    explicit = _write_profiles(tmp_path / "explicit", "redshift")
    env_dir = _write_profiles(tmp_path / "env", "snowflake")
    _write_profiles(root, "duckdb")
    _write_profiles(Path.home() / ".dbt", "bigquery")
    monkeypatch.setenv("DBT_PROFILES_DIR", str(env_dir))

    assert _resolve(root, explicit) == "redshift"
    assert _resolve(root) == "snowflake"
    monkeypatch.delenv("DBT_PROFILES_DIR")
    assert _resolve(root) == "duckdb"
    (root / "profiles.yml").unlink()
    assert _resolve(root) == "bigquery"


def test_resolve_adapter_empty_profiles_dir_env_is_ignored(tmp_path, monkeypatch):
    # click treats an empty environment variable as unset.
    root = _write_profiles(_project_at(tmp_path / "project"), "duckdb")
    monkeypatch.setenv("DBT_PROFILES_DIR", "")
    assert _resolve(root) == "duckdb"


def test_resolve_adapter_relative_profiles_dir_env_is_project_relative(tmp_path, monkeypatch):
    # dbt-refmerge runs dbt from (a snapshot of) the project directory, so dbt resolves it there.
    root = _project_at(tmp_path / "project")
    _write_profiles(root / "config", "snowflake")
    _write_profiles(tmp_path / "config", "bigquery")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DBT_PROFILES_DIR", "config")
    assert _resolve(root) == "snowflake"


def test_resolve_adapter_profiles_dir_env_without_profiles_does_not_fall_back(tmp_path, monkeypatch):
    root = _write_profiles(_project_at(tmp_path / "project"), "duckdb")
    _write_profiles(Path.home() / ".dbt", "bigquery")
    (tmp_path / "empty").mkdir()
    monkeypatch.setenv("DBT_PROFILES_DIR", str(tmp_path / "empty"))
    with pytest.raises(RefmergeError) as ei:
        _resolve(root)
    assert ei.value.reason_code == ReasonCode.UNSUPPORTED_ADAPTER


def test_resolve_adapter_project_profiles_yaml_does_not_select_project_dir(tmp_path):
    root = _project_at(tmp_path / "project")
    (root / "profiles.yaml").write_text("myprof:\n  target: dev\n  outputs:\n    dev:\n      type: duckdb\n")
    _write_profiles(Path.home() / ".dbt", "bigquery")
    assert _resolve(root) == "bigquery"


def test_resolve_adapter_without_profile_pointer_uses_manifest(tmp_path):
    (tmp_path / "dbt_project.yml").write_text("name: p\n")
    _write_profiles(Path.home() / ".dbt", "bigquery")
    spec = resolve_adapter(
        cli_override=None,
        manifest_adapter="postgres",
        project_dir=tmp_path,
        profiles_dir=None,
        profile=None,
        target=None,
    )
    assert spec.name == "postgres"
    with pytest.raises(RefmergeError) as ei:
        _resolve(tmp_path)
    assert ei.value.reason_code == ReasonCode.UNSUPPORTED_ADAPTER
