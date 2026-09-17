"""``python -m dbt_refmerge`` runs the same CLI as the console script."""

import json
import runpy
import subprocess
import sys
from pathlib import Path

import pytest

from dbt_refmerge import __version__


def test_python_dash_m_runs_the_cli(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["dbt_refmerge", "--version"])

    with pytest.raises(SystemExit) as exc_info:
        runpy.run_module("dbt_refmerge", run_name="__main__", alter_sys=True)

    assert exc_info.value.code == 0
    assert capsys.readouterr().out == f"dbt-refmerge {__version__}\n"


def test_python_dash_m_in_a_subprocess_scans_a_project():
    sample = Path(__file__).resolve().parents[1] / "fixtures" / "sample_project"

    result = subprocess.run(
        [sys.executable, "-m", "dbt_refmerge", "scan", "--project-dir", str(sample), "--json"],
        capture_output=True,
        text=True,
        check=True,
    )

    assert json.loads(result.stdout)["summary"] == {"findings": 1}
