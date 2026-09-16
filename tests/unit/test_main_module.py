"""``python -m dbt_refmerge`` runs the same CLI as the console script."""

import runpy
import subprocess
import sys

import pytest

from dbt_refmerge import __version__


def test_python_dash_m_runs_the_cli(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["dbt_refmerge", "--version"])

    with pytest.raises(SystemExit) as exc_info:
        runpy.run_module("dbt_refmerge", run_name="__main__", alter_sys=True)

    assert exc_info.value.code == 0
    assert capsys.readouterr().out == f"dbt-refmerge {__version__}\n"


def test_python_dash_m_in_a_subprocess_names_the_command_in_usage():
    result = subprocess.run(
        [sys.executable, "-m", "dbt_refmerge", "--help"], capture_output=True, text=True, check=True
    )

    assert "Usage: dbt-refmerge [OPTIONS] COMMAND [ARGS]..." in result.stdout
