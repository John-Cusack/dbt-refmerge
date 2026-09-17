"""The pre-commit hook and the GitHub Action call the CLI with arguments it still accepts."""

import shlex
from pathlib import Path

import yaml
from conftest import parse_workflow_command
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
    assert result.stdout.startswith("models/orders.sql:1: ") and "orders, order_financials" in result.stdout


def test_action_interface_and_the_annotation_it_produces_for_the_sample_project(monkeypatch):
    # The ci.yml `action` job runs the action itself; this pins its interface and the scan it relies on.
    action = yaml.safe_load((REPO / "action.yml").read_text(encoding="utf-8"))
    monkeypatch.chdir(REPO)

    result = CliRunner().invoke(
        app, ["scan", "--format", "github", "--project-dir", "tests/fixtures/sample_project", "--fail-on", "never"]
    )

    assert set(action["inputs"]) == {"project-dir", "adapter", "fail-on", "package", "python-version"}
    assert (action["inputs"]["fail-on"]["default"], set(action["outputs"])) == ("never", {"findings"})
    assert result.exit_code == 0, result.stderr
    (annotation,) = (parse_workflow_command(line) for line in result.stdout.splitlines())
    assert annotation.properties["file"] == "tests/fixtures/sample_project/models/orders.sql"
