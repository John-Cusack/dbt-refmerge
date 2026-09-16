"""load_config: .dbt-refmerge.toml, DBT_REFMERGE_* environment, CLI overrides and validation."""

import os
import re
import shlex
from pathlib import Path

import pytest

from dbt_refmerge.config import AppConfig, FailOn, is_secret_name, load_config


def _project(make_project, toml: str | None = None):
    files = {} if toml is None else {".dbt-refmerge.toml": toml}
    return make_project(files)


def test_load_config_defaults_without_toml_or_env(make_project):
    root = _project(make_project)

    assert load_config(root) == AppConfig(project_dir=root.resolve())


def test_load_config_defaults_to_current_directory(make_project, monkeypatch):
    root = _project(make_project, 'profile = "analytics"\n')
    monkeypatch.chdir(root)

    config = load_config()

    assert config.project_dir == root.resolve()
    assert config.profile == "analytics"


def test_load_config_reads_flat_toml(make_project):
    root = _project(
        make_project,
        'profile = "analytics"\n'
        'target = "ci"\n'
        'scratch_schema = "refmerge_scratch"\n'
        'fail_on = "never"\n'
        "subprocess_timeout_seconds = 60\n"
        "keep_workspace = true\n"
        'dbt_command = ["uv", "run", "dbt"]\n',
    )

    config = load_config(root)

    assert (config.profile, config.target, config.scratch_schema) == ("analytics", "ci", "refmerge_scratch")
    assert config.fail_on is FailOn.NEVER
    assert config.subprocess_timeout_seconds == 60
    assert config.keep_workspace is True
    assert config.dbt_command == ("uv", "run", "dbt")


def test_load_config_reads_tool_table(make_project):
    root = _project(
        make_project,
        'profile = "top-level-is-ignored"\n'
        "[tool.sqlfluff]\n"
        'dialect = "postgres"\n'
        "[tool.dbt-refmerge]\n"
        'profile = "analytics"\n',
    )

    assert load_config(root).profile == "analytics"


@pytest.mark.parametrize(
    "toml",
    [
        'tool = "x"\nprofile = "analytics"\n',
        'profile = "analytics"\n[tool.sqlfluff]\ndialect = "postgres"\n',
    ],
    ids=["tool-is-a-string", "tool-table-without-dbt-refmerge"],
)
def test_load_config_ignores_non_table_tool_entry(make_project, toml):
    # §2.3: `tool = "x"` raised AttributeError; a tool entry that is not ours means "no tool table".
    assert load_config(_project(make_project, toml)).profile == "analytics"


def test_load_config_rejects_non_table_dbt_refmerge_entry(make_project):
    # Our own section, malformed: refuse rather than silently ignoring the whole file.
    root = _project(make_project, 'fail_on = "never"\n[tool]\ndbt-refmerge = "x"\n')

    with pytest.raises(ValueError, match=re.escape("[tool.dbt-refmerge] must be a table")):
        load_config(root)


@pytest.mark.parametrize(
    "toml",
    [
        "this is not toml\n",
        'fail_on = "sometimes"\n',
        "subprocess_timeout_seconds = 0\n",
        "dbt_command = 5\n",
        "project_dir = 5\n",
    ],
    ids=["malformed-toml", "unknown-fail-on", "zero-timeout", "dbt-command-not-list-or-string", "project-dir-not-path"],
)
def test_load_config_rejects_invalid_toml_with_value_error(make_project, toml):
    with pytest.raises(ValueError):
        load_config(_project(make_project, toml))


def test_load_config_env_coercions(make_project, monkeypatch):
    root = _project(make_project, 'profile = "from-toml"\ntarget = "from-toml"\n')
    env = {
        "DBT_REFMERGE_PROFILE": "from-env",
        "DBT_REFMERGE_FAIL_ON": "different",
        "DBT_REFMERGE_SUBPROCESS_TIMEOUT_SECONDS": "90",
        "DBT_REFMERGE_WAREHOUSE_STATEMENT_TIMEOUT_MS": "1000",
        "DBT_REFMERGE_WAREHOUSE_LOCK_TIMEOUT_MS": "86400000",
        "DBT_REFMERGE_KEEP_WORKSPACE": "TRUE",
        "DBT_REFMERGE_ALLOW_COMPILE_INTROSPECTION": "yes",
        "DBT_REFMERGE_JSON_OUTPUT": "1",
        "DBT_REFMERGE_DEBUG": "false",
        "REFMERGE_PROFILE": "unprefixed-is-ignored",
    }
    for name, value in env.items():
        monkeypatch.setenv(name, value)

    config = load_config(root)

    assert (config.profile, config.target) == ("from-env", "from-toml")
    assert config.fail_on is FailOn.DIFFERENT
    assert (
        config.subprocess_timeout_seconds,
        config.warehouse_statement_timeout_ms,
        config.warehouse_lock_timeout_ms,
    ) == (90, 1000, 86_400_000)
    assert (config.keep_workspace, config.allow_compile_introspection, config.json_output, config.debug) == (
        True,
        True,
        True,
        False,
    )


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("soon", "subprocess_timeout_seconds"),
        ("0", "timeout must be a positive bounded duration"),
        ("86400001", "timeout must be a positive bounded duration"),
    ],
    ids=["non-integer", "zero", "over-bound"],
)
def test_load_config_env_non_integer_timeout_rejected(make_project, monkeypatch, value, message):
    monkeypatch.setenv("DBT_REFMERGE_SUBPROCESS_TIMEOUT_SECONDS", value)

    with pytest.raises(ValueError, match=message):
        load_config(_project(make_project))


def test_load_config_env_dbt_command_is_shell_split(make_project, monkeypatch, tmp_path):
    # §2.3: str.split broke a quoted executable path containing spaces into several argv entries.
    executable = str(tmp_path / "dbt tools" / "dbt")
    monkeypatch.setenv("DBT_REFMERGE_DBT_COMMAND", f"uv run {shlex.quote(executable)} --no-partial-parse")

    config = load_config(_project(make_project))

    assert config.dbt_command == ("uv", "run", executable, "--no-partial-parse")


@pytest.mark.parametrize(
    ("toml", "expected"),
    [
        ('dbt_command = ["uv", "run", "my dbt"]\n', ("uv", "run", "my dbt")),
        ("dbt_command = \"uv run 'my dbt'\"\n", ("uv", "run", "my dbt")),
    ],
    ids=["list", "string"],
)
def test_load_config_toml_dbt_command_list_and_string(make_project, toml, expected):
    assert load_config(_project(make_project, toml)).dbt_command == expected


def test_load_config_rejects_unbalanced_dbt_command_quotes(make_project, monkeypatch):
    monkeypatch.setenv("DBT_REFMERGE_DBT_COMMAND", '"/opt/dbt tools/dbt')

    with pytest.raises(ValueError, match="dbt_command: No closing quotation"):
        load_config(_project(make_project))


def test_load_config_cli_overrides_win_and_none_ignored(make_project, monkeypatch):
    root = _project(make_project, 'profile = "from-toml"\ndbt_command = "dbt"\n')
    monkeypatch.setenv("DBT_REFMERGE_TARGET", "from-env")
    monkeypatch.setenv("DBT_REFMERGE_DBT_COMMAND", "env-dbt")

    config = load_config(
        root,
        cli_overrides={"profile": "from-cli", "target": None, "dbt_command": ("python", "-m", "dbt")},
    )

    assert (config.profile, config.target) == ("from-cli", "from-env")
    assert config.dbt_command == ("python", "-m", "dbt")


@pytest.mark.parametrize(
    ("field", "max_len"),
    [("profile", 128), ("target", 128), ("scratch_schema", 128), ("adapter", 64)],
)
def test_load_config_validates_profile_target_schema_adapter(make_project, field, max_len):
    root = _project(make_project)

    accepted = "\t" + "a" * (max_len - 1)
    assert getattr(load_config(root, cli_overrides={field: accepted}), field) == accepted
    for bad in ("a\x00b", "a\x1fb", "line\nbreak"):
        with pytest.raises(ValueError, match=f"^{field} contains NUL/control characters$"):
            load_config(root, cli_overrides={field: bad})
    with pytest.raises(ValueError, match=f"^{field} exceeds length limit$"):
        load_config(root, cli_overrides={field: "a" * (max_len + 1)})


def test_load_config_requires_dbt_project_file(tmp_path):
    with pytest.raises(ValueError, match=f"^dbt_project.yml not found at {re.escape(str(tmp_path.resolve()))}$"):
        load_config(tmp_path)


def test_load_config_accepts_dbt_project_yaml(tmp_path):
    (tmp_path / "dbt_project.yaml").write_text("name: p\nprofile: p\n", encoding="utf-8")

    assert load_config(tmp_path).project_dir == tmp_path.resolve()


def test_load_config_rejects_symlinked_project_dir(make_project, tmp_path):
    root = _project(make_project)
    link = tmp_path / "link"
    try:
        os.symlink(root, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")

    with pytest.raises(ValueError, match="^project_dir must not be an untrusted final symlink$"):
        load_config(link)


@pytest.mark.parametrize(
    ("name", "secret"),
    [
        ("DBT_ENV_SECRET_X", True),
        ("PGPASSWORD", True),
        ("GitHub_Token", True),
        ("api_key", True),
        ("profile", False),
        ("DBT_TARGET", False),
    ],
)
def test_is_secret_name_matches_hints_case_insensitively(name, secret):
    assert is_secret_name(name) is secret


def test_load_config_resolves_relative_profiles_dir_against_the_working_directory(make_project, monkeypatch, tmp_path):
    # dbt runs inside a snapshot elsewhere, so a relative --profiles-dir must be made absolute up front.
    root = make_project({})
    (tmp_path / "work").mkdir()
    monkeypatch.chdir(tmp_path / "work")

    config = load_config(root, cli_overrides={"profiles_dir": Path("../profiles")})

    assert config.profiles_dir == (tmp_path / "profiles").resolve()


@pytest.mark.parametrize("env_value", ["conf", "{absolute}"], ids=["relative", "absolute"])
def test_load_config_uses_dbt_profiles_dir_resolved_like_dbt(make_project, monkeypatch, tmp_path, env_value):
    # dbt resolves a relative DBT_PROFILES_DIR against its working directory: the project root.
    root = make_project({})
    absolute = tmp_path / "shared-profiles"
    monkeypatch.setenv("DBT_PROFILES_DIR", env_value.format(absolute=absolute))

    config = load_config(root)

    assert config.profiles_dir == (absolute if env_value.startswith("{") else root.resolve() / "conf")


def test_load_config_explicit_profiles_dir_wins_over_dbt_profiles_dir(make_project, monkeypatch, tmp_path):
    root = make_project({})
    monkeypatch.setenv("DBT_PROFILES_DIR", str(tmp_path / "env"))

    config = load_config(root, cli_overrides={"profiles_dir": tmp_path / "cli"})

    assert config.profiles_dir == tmp_path / "cli"
