"""dbt CLI wrapper, in memory: output parsing and argument handling."""

import pytest

from dbt_refmerge.dbt_cli import DbtCli, _redact_argv, parse_dbt_version_output
from dbt_refmerge.errors import DbtError

DBT_1_12 = (
    "Core:\n"
    "  - installed: 1.12.5\n"
    "  - latest:    1.12.5 - Up to date!\n"
    "\n"
    "Plugins:\n"
    "  - postgres: 1.11.0 - Up to date!\n"
)


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        (DBT_1_12, "1.12.5"),
        ("Core:\n  - latest: 1.9.0\n  - installed: 1.8.2\n", "1.8.2"),
        ("Core: 1.4.9\nPlugins:\n", "1.4.9"),
        ("installed version: 1.2.0\n  latest version: 1.2.0\n", "installed version: 1.2.0"),
        ("Core:\nPlugins:\n  - postgres: 1.8.0\n", "Core:"),
        ("", ""),
    ],
    ids=["dbt-1.5+", "installed-not-first", "pre-1.5", "unknown-format", "core-without-installed", "empty"],
)
def test_parse_dbt_version_output(output, expected):
    assert parse_dbt_version_output(output) == expected


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["dbt", "compile", "--password", "hunter2"], ("dbt", "compile", "--password", "***")),
        (["dbt", "--token=abc"], ("dbt", "--token=***")),
        (["dbt", "--client-secret", "s", "--select", "m"], ("dbt", "--client-secret", "***", "--select", "m")),
        (["dbt", "--vars", '{"api_key": "k"}'], ("dbt", "--vars", "***")),
        (["dbt", "--vars", '{"day": "2026-01-01"}'], ("dbt", "--vars", '{"day": "2026-01-01"}')),
        (["dbt", "compile", "--select", "secret_model"], ("dbt", "compile", "--select", "secret_model")),
    ],
    ids=["flag-value", "flag-equals", "hyphenated", "vars-with-secret", "vars-plain", "positional"],
)
def test_redact_argv_masks_secret_values(argv, expected):
    # S11
    assert _redact_argv(argv) == expected


def test_launch_failure_error_carries_redacted_argv(tmp_path):
    missing = str(tmp_path / "missing-dbt")
    with pytest.raises(DbtError) as exc_info:
        DbtCli((missing, "--password", "hunter2")).version(cwd=tmp_path)
    assert exc_info.value.argv == (missing, "--password", "***", "--version")


def test_dbt_cli_rejects_empty_command():
    with pytest.raises(DbtError) as exc_info:
        DbtCli(())
    assert str(exc_info.value) == "[DBT_COMMAND_FAILED] empty dbt command"
    assert exc_info.value.argv == ()
