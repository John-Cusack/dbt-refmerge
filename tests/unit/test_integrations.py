"""The pre-commit hook and the GitHub Action call the CLI with arguments it still accepts."""

import shlex
from pathlib import Path

import yaml
from typer.testing import CliRunner

from dbt_refmerge.cli import app

REPO = Path(__file__).resolve().parents[2]
SAMPLE_PROJECT = REPO / "tests" / "fixtures" / "sample_project"


def test_pre_commit_hook_fails_on_the_sample_projects_duplicate_import(monkeypatch):
    (hook,) = yaml.safe_load((REPO / ".pre-commit-hooks.yaml").read_text(encoding="utf-8"))
    command, *args = shlex.split(hook["entry"])
    monkeypatch.chdir(SAMPLE_PROJECT)  # pre-commit runs hooks from the repository root

    result = CliRunner().invoke(app, args)

    assert (command, hook["id"], hook["pass_filenames"]) == ("dbt-refmerge", "dbt-refmerge-scan", False)
    assert result.exit_code == 2, result.stderr
    assert result.stdout == (
        "models/orders.sql:1: CTEs orders, order_financials import the same relation; "
        "run dbt-refmerge check to prove a merge\n"
    )


def test_action_scans_with_github_annotations(monkeypatch):
    action = yaml.safe_load((REPO / "action.yml").read_text(encoding="utf-8"))
    script = action["runs"]["steps"][-1]["run"]
    monkeypatch.chdir(REPO)

    result = CliRunner().invoke(
        app, ["scan", "--format", "github", "--project-dir", "tests/fixtures/sample_project", "--fail-on", "never"]
    )

    assert 'args=(scan --format github --project-dir "$PROJECT_DIR" --fail-on "$FAIL_ON")' in script
    assert result.exit_code == 0, result.stderr
    assert result.stdout.startswith(
        "::warning file=tests/fixtures/sample_project/models/orders.sql,line=1,title=dbt-refmerge%3A duplicate"
    )
    assert action["outputs"]["findings"]["value"] == "${{ steps.scan.outputs.findings }}"
