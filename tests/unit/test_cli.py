"""CLI commands that need no dbt: scan, configuration errors, output fidelity and exit codes."""

import json

from typer.testing import CliRunner

from dbt_refmerge.cli import app

DUPLICATE = (
    "with a as (\n    select\n        id,\n        customer_id\n    from {{ ref('stg') }}\n),\n"
    "b as (\n    select\n        id,\n        amount\n    from {{ ref('stg') }}\n)\n"
    "select a.customer_id, b.amount from a join b using (id)\n"
)


def _invoke(*args: str):
    return CliRunner().invoke(app, list(args))


def test_scan_prints_one_lead_per_line_with_the_project_relative_path(make_project):
    # Rich used to swallow "[legacy]" as markup and wrap long lines at 80 columns.
    long_dir = "models/[legacy]/" + "a_very_long_directory_name_" * 3
    root = make_project({f"{long_dir}/orders.sql": DUPLICATE})

    result = _invoke("scan", "--project-dir", str(root), "--adapter", "postgres")

    assert result.exit_code == 0, result.stderr
    assert result.stdout == f"{long_dir}/orders.sql: a, b -> ?\n"


def test_scan_json_lists_findings(make_project):
    root = make_project({"models/orders.sql": DUPLICATE, "models/clean.sql": "select 1\n"})

    result = _invoke("scan", "--project-dir", str(root), "--adapter", "postgres", "--json")

    payload = json.loads(result.stdout)
    assert (payload["command"], payload["summary"]) == ("scan", {"findings": 1})
    assert payload["findings"][0]["cte_names"] == ["a", "b"]


def test_scan_without_findings_prints_nothing(make_project):
    root = make_project({"models/clean.sql": "select 1\n"})
    result = _invoke("scan", "--project-dir", str(root), "--adapter", "postgres")
    assert (result.exit_code, result.stdout) == (0, "")


def test_scan_fail_on_finding_exits_2_only_when_there_are_findings(make_project):
    root = make_project({"models/orders.sql": DUPLICATE})
    assert _invoke("scan", "--project-dir", str(root), "--adapter", "postgres", "--fail-on", "finding").exit_code == 2
    assert _invoke("scan", "--project-dir", str(root), "--adapter", "postgres").exit_code == 0


def test_unset_flags_do_not_override_config_file(make_project):
    # Typer defaults used to be passed as overrides, silently replacing .dbt-refmerge.toml values.
    root = make_project(
        {"models/orders.sql": DUPLICATE, ".dbt-refmerge.toml": 'fail_on = "finding"\njson_output = true\n'}
    )

    result = _invoke("scan", "--project-dir", str(root), "--adapter", "postgres")

    assert result.exit_code == 2
    assert json.loads(result.stdout)["summary"] == {"findings": 1}


def test_configuration_error_exits_1(tmp_path):
    result = _invoke("scan", "--project-dir", str(tmp_path))
    assert result.exit_code == 1
    assert result.stderr.startswith("configuration error: dbt_project.yml not found")


def test_operational_error_exits_1_and_debug_adds_the_traceback(make_project):
    root = make_project({"models/orders.sql": DUPLICATE})

    plain = _invoke("scan", "--project-dir", str(root), "--profiles-dir", str(root / "none"))
    debug = _invoke("scan", "--project-dir", str(root), "--profiles-dir", str(root / "none"), "--debug")

    assert plain.exit_code == debug.exit_code == 1
    assert plain.stderr.startswith("scan failed: [UNSUPPORTED_ADAPTER]")
    assert "Traceback" not in plain.stderr and "Traceback (most recent call last)" in debug.stderr


def test_check_requires_a_scratch_schema(make_project):
    root = make_project({"models/orders.sql": DUPLICATE})
    result = _invoke("check", "--project-dir", str(root), "--adapter", "postgres")
    assert result.exit_code == 1
    assert result.stderr.startswith("check failed: [SCRATCH_BOUNDARY_VIOLATION]")


def test_fix_requires_a_scratch_schema(make_project):
    root = make_project({"models/orders.sql": DUPLICATE})
    result = _invoke("fix", "models/orders.sql", "--project-dir", str(root), "--adapter", "postgres")
    assert result.exit_code == 1
    assert result.stderr.startswith("fix failed: [SCRATCH_BOUNDARY_VIOLATION]")


def test_cleanup_rejects_a_malformed_run_id(make_project):
    root = make_project({})
    result = _invoke("cleanup", "--run-id", "nope", "--project-dir", str(root), "--scratch-schema", "s")
    assert result.exit_code == 1
    assert result.stderr.startswith("cleanup failed: [CLEANUP_FAILED] invalid run id")


def test_removed_flags_are_rejected(make_project):
    root = make_project({})
    for command, flag in (("scan", "--compile"), ("scan", "--select"), ("check", "--allow-compile-introspection")):
        assert _invoke(command, "--project-dir", str(root), flag).exit_code == 2
