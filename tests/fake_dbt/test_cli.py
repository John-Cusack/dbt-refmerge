"""CLI end to end against the fake dbt: check, fix and cleanup output and exit codes."""

import json
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from dbt_refmerge.cli import app

pytestmark = pytest.mark.fake_dbt

MODEL = (
    "with a as (\n    select\n        id,\n        customer_id\n    from {{ ref('stg') }}\n),\n"
    "b as (\n    select\n        id,\n        amount\n    from {{ ref('stg') }}\n)\n"
    "select a.customer_id, b.amount from a join b using (id)\n"
)
STG = "select 1 as id, 2 as customer_id, 3 as amount\n"


@pytest.fixture
def project(make_project, fake_dbt, tmp_path):
    root = make_project({"models/stg.sql": STG, "models/orders.sql": MODEL})
    common = [
        "--project-dir",
        str(root),
        "--adapter",
        "postgres",
        "--profiles-dir",
        str(tmp_path / "none"),
        "--scratch-schema",
        "refmerge_scratch",
        *[arg for part in fake_dbt.command for arg in ("--dbt-command-part", part)],
    ]
    return root, common


def _invoke(*args: str):
    return CliRunner().invoke(app, list(args))


def test_check_json_reports_dbt_metadata_and_exits_2_for_fixable_merges(project):
    root, common = project

    result = _invoke("check", *common, "--json")

    assert result.exit_code == 2, result.stderr
    payload = json.loads(result.stdout)
    assert payload["dbt"] == {
        "version": "1.9.0",
        "adapter_type": "postgres",
        "manifest_schema_version": "https://schemas.getdbt.com/dbt/manifest/v12.json",
    }
    statuses = {m["model_unique_id"]: (m["status"], m["fixable"]) for m in payload["models"]}
    assert statuses == {"model.p.orders": ("snapshot_equivalent", True), "model.p.stg": ("not_run", False)}
    assert payload["cleanup"]["complete"] and payload["workspace"] is None


def test_check_human_output_and_fail_on_never(project):
    _root, common = project

    result = _invoke("check", *common, "--fail-on", "never")

    assert result.exit_code == 0
    assert "model model.p.orders (models/orders.sql)" in result.stdout
    assert "  status: snapshot_equivalent fixable=True" in result.stdout
    assert "+select a.customer_id, b.amount from a join a as b using (id)" in result.stdout


def test_human_mode_reports_progress_on_stderr_and_json_mode_does_not(project):
    root, common = project

    human = _invoke("check", *common, "--fail-on", "never")
    as_json = _invoke("check", *common, "--json", "--fail-on", "never")
    fixed = _invoke("fix", "models/orders.sql", *common)

    assert human.stderr.splitlines() == [
        "dbt-refmerge: copying the project into a private workspace",
        "dbt-refmerge: compiling the project with dbt (--select fqn:*)",
        "dbt-refmerge: analyzing 2 models",
        "dbt-refmerge: compiling 1 candidate merge",
        "dbt-refmerge: verifying 1 merge on the warehouse (scratch schema refmerge_scratch; views are dropped "
        "afterwards)",
    ]
    assert "dbt-refmerge:" not in human.stdout
    assert as_json.stderr == "" and json.loads(as_json.stdout)["models"]
    assert fixed.exit_code == 0
    assert fixed.stderr.splitlines()[-1] == "dbt-refmerge: writing the verified merge to models/orders.sql"
    assert "join a as b" in (root / "models" / "orders.sql").read_text()


def test_check_select_uses_dbt_selection(project, fake_dbt):
    _root, common = project

    result = _invoke("check", *common, "--select", "stg", "--json")

    assert [m["model_unique_id"] for m in json.loads(result.stdout)["models"]] == ["model.p.stg"]
    compiles = [call for call in fake_dbt.calls() if call[0] == "compile" and "--select" in call]
    assert compiles[0][compiles[0].index("--select") + 1] == "stg"


def test_check_keep_workspace_reports_where_it_is(project):
    _root, common = project

    result = _invoke("check", *common, "--keep-workspace", "--json", "--fail-on", "never")

    workspace = Path(json.loads(result.stdout)["workspace"])
    assert (workspace / "run-ledger.json").is_file()
    human = _invoke("check", *common, "--keep-workspace", "--fail-on", "never")
    assert human.stdout.rstrip().splitlines()[-1].startswith("workspace kept at ")


def test_fix_human_output_without_dry_run_has_no_diff(project):
    _root, common = project

    result = _invoke("fix", "models/orders.sql", *common)

    assert (result.exit_code, result.stdout) == (0, "applied=True dry_run=False applied\n")


def test_fix_dry_run_prints_the_diff_and_leaves_the_file(project):
    root, common = project

    result = _invoke("fix", "models/orders.sql", *common, "--dry-run")

    assert result.exit_code == 0, result.stderr
    assert "--- a/models/orders.sql" in result.stdout
    assert "+select a.customer_id, b.amount from a join a as b using (id)" in result.stdout
    assert result.stdout.rstrip().endswith("applied=False dry_run=True dry-run")
    assert (root / "models" / "orders.sql").read_text() == MODEL


def test_fix_applies_and_reports_json(project):
    root, common = project

    result = _invoke("fix", "models/orders.sql", *common, "--json")

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert (payload["applied"], payload["status"], payload["reason_codes"]) == (True, "snapshot_equivalent", ["OK"])
    assert "join a as b using (id)" in (root / "models" / "orders.sql").read_text()


def test_fix_checks_only_the_requested_model(project, fake_dbt):
    _root, common = project

    _invoke("fix", "models/orders.sql", *common, "--dry-run")

    compiles = [call for call in fake_dbt.calls() if call[0] == "compile" and "--select" in call]
    assert compiles[0][compiles[0].index("--select") + 1] == "path:models/orders.sql"


@pytest.mark.parametrize("model_path", ["models/missing.sql", "orders.sql", "rders.sql"])
def test_fix_unknown_or_partial_path_is_model_not_found(project, model_path):
    _root, common = project

    result = _invoke("fix", model_path, *common)

    assert result.exit_code == 1
    assert result.stderr.splitlines()[-1] == "fix: model not found"


def test_fix_accepts_an_absolute_model_path(project):
    root, common = project
    result = _invoke("fix", str(root / "models" / "orders.sql"), *common, "--dry-run")
    assert result.exit_code == 0, result.stderr


def test_fix_refused_exit_code_follows_the_verdict(project, monkeypatch):
    root, common = project
    monkeypatch.setenv("FAKE_DBT_VERDICT", "2,1,1,0")

    result = _invoke("fix", "models/orders.sql", *common, "--json")

    assert result.exit_code == 3
    payload = json.loads(result.stdout)
    assert (payload["applied"], payload["status"], payload["reason"]) == (False, "different", "not fixable")
    assert (root / "models" / "orders.sql").read_text() == MODEL


@pytest.mark.skipif(sys.platform == "win32", reason="SIGINT delivery to the test process is POSIX-only")
def test_interrupt_exits_130(project, fake_dbt):
    _root, common = project
    fake_dbt.set_mode("sigint_parent=0.2", "sleep=30")

    result = _invoke("check", *common)

    assert (result.exit_code, result.stderr.splitlines()[-1]) == (130, "interrupted")


def test_cleanup_prints_json_and_exits_1_when_views_remain(project, monkeypatch):
    _root, common = project
    run_id = "20260916T120000_0123456789ab"
    monkeypatch.setenv("FAKE_DBT_RUN_VIEWS", "dbt_refmerge_baseline_000_0123456789ab_deadbeef")
    connection = [a for a in common if a not in ("--adapter", "postgres")]

    done = _invoke("cleanup", "--run-id", run_id, *connection)
    monkeypatch.setenv("FAKE_DBT_REMAINING", "all")
    stuck = _invoke("cleanup", "--run-id", run_id, *connection)

    assert done.exit_code == 0 and json.loads(done.stdout)["dropped"] == [
        "dbt_refmerge_baseline_000_0123456789ab_deadbeef"
    ]
    assert stuck.exit_code == 1 and json.loads(stuck.stdout)["complete"] is False
