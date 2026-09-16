"""DbtCli against the fake dbt: argv assembly, capability discovery, timeouts, interrupts and log capture."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from dbt_refmerge.dbt_cli import MAX_RETAINED_LOG_BYTES, DbtCli, DbtCliCapabilities, DbtInvocation, _kill, _terminate
from dbt_refmerge.errors import DbtError

pytestmark = pytest.mark.fake_dbt

POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups and signals")


def _inv(tmp_path: Path, **kwargs) -> DbtInvocation:
    defaults = {"project_dir": tmp_path, "profiles_dir": None, "profile": None, "target": None, "target_path": None}
    return DbtInvocation(**{**defaults, **kwargs})


def _project(make_project):
    return make_project({"models/m.sql": "select 1 as id\n"})


def test_version_nonzero_exit_raises(fake_dbt, tmp_path):
    fake_dbt.set_mode("version_fail")
    with pytest.raises(DbtError, match="dbt --version failed"):
        DbtCli(fake_dbt.command).version(cwd=tmp_path)


def test_version_keeps_raw_output(fake_dbt, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_DBT_VERSION_OUTPUT", "installed version: 1.2.0\n")
    version = DbtCli(fake_dbt.command).version(cwd=tmp_path)
    assert (version.raw, version.version) == ("installed version: 1.2.0", "installed version: 1.2.0")


@pytest.mark.parametrize(
    ("help_text", "expected"),
    [
        (None, DbtCliCapabilities(True, True, True, True)),
        ("--target-path TEXT\n", DbtCliCapabilities(False, False, False, True)),
        ("", DbtCliCapabilities(False, False, False, False)),
    ],
    ids=["dbt-1.9", "target-path-only", "nothing"],
)
def test_discover_capabilities_from_compile_help(fake_dbt, tmp_path, monkeypatch, help_text, expected):
    if help_text is not None:
        monkeypatch.setenv("FAKE_DBT_COMPILE_HELP", help_text)
    dbt = DbtCli(fake_dbt.command)

    assert dbt.discover_capabilities(cwd=tmp_path) == expected
    assert fake_dbt.calls() == [["compile", "--help"]]


def test_compile_argv_with_every_option(make_project, fake_dbt, tmp_path):
    root = _project(make_project)
    dbt = DbtCli(fake_dbt.command, DbtCliCapabilities(True, True, True, True))
    inv = _inv(
        tmp_path,
        project_dir=root,
        profiles_dir=tmp_path / "profiles",
        profile="prof",
        target="ci",
        target_path=tmp_path / "target",
        threads=2,
        vars_json='{"day": "2026-01-01"}',
        extra_args=("--no-introspect",),
    )

    result = dbt.compile(inv, "fqn:m")

    assert result.returncode == 0
    assert fake_dbt.calls()[-1] == [
        "compile",
        "--project-dir",
        str(root),
        "--profiles-dir",
        str(tmp_path / "profiles"),
        "--profile",
        "prof",
        "--target",
        "ci",
        "--target-path",
        str(tmp_path / "target"),
        "--no-partial-parse",
        "--no-populate-cache",
        "--threads",
        "2",
        "--vars",
        '{"day": "2026-01-01"}',
        "--select",
        "fqn:m",
        "--no-introspect",
    ]


def test_compile_argv_minimal(make_project, fake_dbt, tmp_path):
    root = _project(make_project)
    DbtCli(fake_dbt.command, DbtCliCapabilities(False, False, False, False)).compile(
        _inv(tmp_path, project_dir=root), "fqn:*"
    )
    assert fake_dbt.calls()[-1] == ["compile", "--project-dir", str(root), "--select", "fqn:*"]


def test_parse_run_and_run_operation_argv(make_project, fake_dbt, tmp_path):
    root = _project(make_project)
    dbt = DbtCli(fake_dbt.command, DbtCliCapabilities(True, False, False, True))
    inv = _inv(
        tmp_path,
        project_dir=root,
        profiles_dir=tmp_path / "p",
        profile="prof",
        target="ci",
        target_path=tmp_path / "t",
        vars_json='{"v": 1}',
    )

    dbt.parse(inv)
    dbt.run(inv, "fqn:*")
    dbt.run_operation(inv, "dbt_refmerge_drop_views", {"schema": "s", "identifiers": []})

    common = ["--project-dir", str(root), "--profiles-dir", str(tmp_path / "p"), "--profile", "prof", "--target", "ci"]
    assert fake_dbt.calls() == [
        ["parse", *common, "--no-partial-parse", "--target-path", str(tmp_path / "t"), "--vars", '{"v": 1}'],
        [
            "run",
            *common,
            "--target-path",
            str(tmp_path / "t"),
            "--vars",
            '{"v": 1}',
            "--threads",
            "1",
            "--select",
            "fqn:*",
        ],
        [
            "run-operation",
            "dbt_refmerge_drop_views",
            *common,
            "--target-path",
            str(tmp_path / "t"),
            "--vars",
            '{"v": 1}',
            "--args",
            json.dumps({"schema": "s", "identifiers": []}),
        ],
    ]


def test_invocation_env_overrides_reach_dbt(make_project, fake_dbt, tmp_path):
    root = _project(make_project)
    env = {"FAKE_DBT_MODE": "echo_env=REFMERGE_PROBE", "REFMERGE_PROBE": "from-invocation"}

    result = DbtCli(fake_dbt.command).compile(_inv(tmp_path, project_dir=root, env=env), "fqn:*")

    assert "REFMERGE_PROBE=from-invocation" in result.stdout


@pytest.mark.parametrize(
    ("command", "invoke"),
    [
        ("compile", lambda dbt, inv: dbt.compile(inv, "fqn:*")),
        ("run", lambda dbt, inv: dbt.run(inv, "fqn:*")),
        ("run-operation", lambda dbt, inv: dbt.run_operation(inv, "m")),
    ],
)
def test_timeout_raises_dbt_error(make_project, fake_dbt, tmp_path, command, invoke):
    fake_dbt.set_mode("sleep=30")
    started = time.monotonic()
    with pytest.raises(DbtError, match=f"dbt {command} timed out"):
        invoke(DbtCli(fake_dbt.command), _inv(tmp_path, project_dir=_project(make_project), timeout_seconds=1))
    assert time.monotonic() - started < 10


@POSIX_ONLY
def test_timeout_kills_a_child_that_ignores_sigterm(make_project, fake_dbt, tmp_path):
    fake_dbt.set_mode("ignore_sigterm", "sleep=60")
    dbt = DbtCli(fake_dbt.command, terminate_grace_seconds=0.5)
    started = time.monotonic()

    with pytest.raises(DbtError, match="timed out"):
        dbt.compile(_inv(tmp_path, project_dir=_project(make_project), timeout_seconds=1), "fqn:*")

    assert time.monotonic() - started < 10


@POSIX_ONLY
def test_keyboard_interrupt_stops_and_reaps_the_child(make_project, fake_dbt, tmp_path, monkeypatch):
    pid_file = tmp_path / "dbt.pid"
    monkeypatch.setenv("FAKE_DBT_PID_FILE", str(pid_file))
    fake_dbt.set_mode("sigint_parent=0.3", "sleep=60")

    with pytest.raises(KeyboardInterrupt):
        DbtCli(fake_dbt.command, terminate_grace_seconds=1).compile(
            _inv(tmp_path, project_dir=_project(make_project)), "fqn:*"
        )

    with pytest.raises(ChildProcessError):  # already reaped: no zombie, no leaked pipes
        os.waitpid(int(pid_file.read_text()), os.WNOHANG)


@pytest.mark.parametrize("char", ["o", "é"], ids=["ascii", "two-byte"])
def test_retained_logs_are_capped_in_bytes(make_project, fake_dbt, tmp_path, char):
    fake_dbt.set_mode(f"big_logs={MAX_RETAINED_LOG_BYTES + 100_000}", f"big_logs_char={char}")

    result = DbtCli(fake_dbt.command).compile(_inv(tmp_path, project_dir=_project(make_project)), "fqn:*")

    for stream in (result.stdout, result.stderr):
        assert MAX_RETAINED_LOG_BYTES - 4 <= len(stream.encode("utf-8")) <= MAX_RETAINED_LOG_BYTES


def test_undecodable_output_is_replaced_not_raised(make_project, fake_dbt, tmp_path):
    fake_dbt.set_mode("invalid_utf8")

    result = DbtCli(fake_dbt.command).compile(_inv(tmp_path, project_dir=_project(make_project)), "fqn:*")

    assert "before �� after" in result.stdout


def test_terminate_and_kill_tolerate_an_exited_process():
    proc = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
    proc.wait()
    _terminate(proc)
    _kill(proc)


def test_parse_argv_minimal(make_project, fake_dbt, tmp_path):
    root = _project(make_project)
    DbtCli(fake_dbt.command, DbtCliCapabilities(False, False, False, False)).parse(_inv(tmp_path, project_dir=root))
    assert fake_dbt.calls()[-1] == ["parse", "--project-dir", str(root)]
