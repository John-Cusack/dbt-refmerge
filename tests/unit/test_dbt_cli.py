"""dbt CLI wrapper, in memory: output parsing and argument handling."""

import pytest

from dbt_refmerge.dbt_cli import parse_dbt_version_output

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
