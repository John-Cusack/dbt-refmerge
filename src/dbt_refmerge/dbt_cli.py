"""Subprocess wrapper for user-selected dbt executable. No dbt Python dependency."""

from __future__ import annotations

import os
import shlex
import signal
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dbt_refmerge.config import redact_mapping
from dbt_refmerge.errors import DbtError

JSONValue = Any

MAX_RETAINED_LOG_BYTES = 1_000_000


@dataclass(frozen=True)
class CommandResult:
    argv_redacted: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool


@dataclass(frozen=True)
class DbtInvocation:
    project_dir: Path
    profiles_dir: Path | None
    profile: str | None
    target: str | None
    target_path: Path | None
    threads: int | None = None
    vars_json: str | None = None
    extra_args: tuple[str, ...] = ()
    env: Mapping[str, str] | None = None
    timeout_seconds: int = 1800


@dataclass(frozen=True)
class DbtVersion:
    raw: str
    version: str


@dataclass(frozen=True)
class DbtCliCapabilities:
    supports_no_partial_parse: bool = False
    supports_no_populate_cache: bool = False
    supports_no_introspect: bool = False
    supports_target_path: bool = True


def _redact_argv(argv: list[str]) -> tuple[str, ...]:
    out: list[str] = []
    skip_next = False
    for tok in argv:
        if skip_next:
            out.append("***")
            skip_next = False
            continue
        low = tok.lower()
        if any(h in low for h in ("password", "token", "secret")) and "=" not in tok:
            out.append(tok)
            continue
        out.append(tok)
    return tuple(out)


class DbtCli:
    def __init__(self, command: tuple[str, ...], capabilities: DbtCliCapabilities | None = None) -> None:
        if not command:
            raise DbtError("empty dbt command", argv=())
        self._command = tuple(command)
        self._capabilities = capabilities or DbtCliCapabilities()

    @classmethod
    def from_config(cls, dbt_command: tuple[str, ...]) -> DbtCli:
        return cls(tuple(dbt_command))

    @property
    def capabilities(self) -> DbtCliCapabilities:
        return self._capabilities

    def _run_argv(
        self,
        argv: list[str],
        cwd: Path,
        env: Mapping[str, str] | None,
        timeout_seconds: int,
    ) -> CommandResult:
        import time

        full_env = dict(os.environ)
        if env is not None:
            full_env.update(dict(env))
        start = time.monotonic()
        try:
            proc = subprocess.Popen(
                argv,
                cwd=str(cwd),
                env=full_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                shell=False,
                start_new_session=True,
            )
        except OSError as exc:
            raise DbtError(f"failed to launch dbt: {exc}", argv=tuple(argv)) from exc
        timed_out = False
        try:
            stdout, stderr = proc.communicate(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                stdout, stderr = proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
                stdout, stderr = proc.communicate()
        except KeyboardInterrupt:
            try:
                os.killpg(proc.pid, signal.SIGINT)
            except (ProcessLookupError, PermissionError):
                pass
            raise
        duration = time.monotonic() - start
        # cap retained bytes
        if len(stdout.encode("utf-8", "ignore")) > MAX_RETAINED_LOG_BYTES:
            stdout = stdout[-MAX_RETAINED_LOG_BYTES:]
        if len(stderr.encode("utf-8", "ignore")) > MAX_RETAINED_LOG_BYTES:
            stderr = stderr[-MAX_RETAINED_LOG_BYTES:]
        _ = redact_mapping(dict(full_env))
        return CommandResult(
            argv_redacted=_redact_argv(argv),
            returncode=proc.returncode,
            stdout=stdout,
            stderr=stderr,
            duration_seconds=duration,
            timed_out=timed_out,
        )

    def _base_argv(self, inv: DbtInvocation) -> list[str]:
        argv = list(self._command)
        return argv

    def version(self, cwd: Path | None = None) -> DbtVersion:
        res = self._run_argv([*self._command, "--version"], cwd=cwd or Path.cwd(), env=None, timeout_seconds=120)
        if res.timed_out or res.returncode != 0:
            raise DbtError("dbt --version failed", argv=res.argv_redacted)
        text = res.stdout.strip()
        ver = ""
        for line in text.splitlines():
            line = line.strip()
            if line.lower().startswith("core:"):
                ver = line.split(":", 1)[1].strip().split()[0]
                break
        return DbtVersion(raw=text, version=ver or text.splitlines()[0] if text else "")

    def discover_capabilities(self, cwd: Path | None = None) -> DbtCliCapabilities:
        res = self._run_argv([*self._command, "--help"], cwd=cwd or Path.cwd(), env=None, timeout_seconds=120)
        help_text = (res.stdout + res.stderr).lower()
        res2 = self._run_argv(
            [*self._command, "compile", "--help"],
            cwd=cwd or Path.cwd(),
            env=None,
            timeout_seconds=120,
        )
        compile_help = (res2.stdout + res2.stderr).lower()
        caps = DbtCliCapabilities(
            supports_no_partial_parse="no-partial-parse" in compile_help,
            supports_no_populate_cache="no-populate-cache" in compile_help,
            supports_no_introspect="no-introspect" in compile_help or "introspect" in compile_help,
            supports_target_path="target-path" in compile_help,
        )
        self._capabilities = caps
        _ = help_text
        return caps

    def parse(self, invocation: DbtInvocation) -> CommandResult:
        argv = self._base_argv(invocation)
        argv += ["parse", "--project-dir", str(invocation.project_dir)]
        if invocation.profiles_dir is not None:
            argv += ["--profiles-dir", str(invocation.profiles_dir)]
        if invocation.profile:
            argv += ["--profile", invocation.profile]
        if invocation.target:
            argv += ["--target", invocation.target]
        if self._capabilities.supports_no_partial_parse:
            argv += ["--no-partial-parse"]
        if invocation.target_path is not None:
            argv += ["--target-path", str(invocation.target_path)]
        argv += list(invocation.extra_args)
        return self._run_argv(
            argv,
            cwd=invocation.project_dir,
            env=invocation.env,
            timeout_seconds=invocation.timeout_seconds,
        )

    def compile(self, invocation: DbtInvocation, selector: str) -> CommandResult:
        argv = self._base_argv(invocation)
        argv += ["compile", "--project-dir", str(invocation.project_dir)]
        if invocation.profiles_dir is not None:
            argv += ["--profiles-dir", str(invocation.profiles_dir)]
        if invocation.profile:
            argv += ["--profile", invocation.profile]
        if invocation.target:
            argv += ["--target", invocation.target]
        if invocation.target_path is not None:
            argv += ["--target-path", str(invocation.target_path)]
        if self._capabilities.supports_no_partial_parse:
            argv += ["--no-partial-parse"]
        if self._capabilities.supports_no_populate_cache:
            argv += ["--no-populate-cache"]
        if invocation.threads is not None:
            argv += ["--threads", str(invocation.threads)]
        if invocation.vars_json:
            argv += ["--vars", invocation.vars_json]
        # introspection default off is handled by caller passing --no-introspect when supported
        argv += ["--select", selector]
        argv += list(invocation.extra_args)
        res = self._run_argv(
            argv,
            cwd=invocation.project_dir,
            env=invocation.env,
            timeout_seconds=invocation.timeout_seconds,
        )
        if res.timed_out:
            raise DbtError("dbt compile timed out", argv=res.argv_redacted)
        return res

    def run(self, invocation: DbtInvocation, selector: str) -> CommandResult:
        argv = self._base_argv(invocation)
        argv += ["run", "--project-dir", str(invocation.project_dir)]
        if invocation.profiles_dir is not None:
            argv += ["--profiles-dir", str(invocation.profiles_dir)]
        if invocation.profile:
            argv += ["--profile", invocation.profile]
        if invocation.target:
            argv += ["--target", invocation.target]
        if invocation.target_path is not None:
            argv += ["--target-path", str(invocation.target_path)]
        argv += ["--threads", str(invocation.threads or 1), "--select", selector]
        argv += list(invocation.extra_args)
        res = self._run_argv(
            argv,
            cwd=invocation.project_dir,
            env=invocation.env,
            timeout_seconds=invocation.timeout_seconds,
        )
        if res.timed_out:
            raise DbtError("dbt run timed out", argv=res.argv_redacted)
        return res

    def run_operation(
        self,
        invocation: DbtInvocation,
        macro_name: str,
        args: Mapping[str, JSONValue] | None = None,
    ) -> CommandResult:
        import json as _json

        argv = self._base_argv(invocation)
        argv += ["run-operation", macro_name, "--project-dir", str(invocation.project_dir)]
        if invocation.profiles_dir is not None:
            argv += ["--profiles-dir", str(invocation.profiles_dir)]
        if invocation.profile:
            argv += ["--profile", invocation.profile]
        if invocation.target:
            argv += ["--target", invocation.target]
        if invocation.target_path is not None:
            argv += ["--target-path", str(invocation.target_path)]
        if args:
            argv += ["--args", _json.dumps(dict(args))]
        argv += list(invocation.extra_args)
        res = self._run_argv(
            argv,
            cwd=invocation.project_dir,
            env=invocation.env,
            timeout_seconds=invocation.timeout_seconds,
        )
        if res.timed_out:
            raise DbtError("dbt run-operation timed out", argv=res.argv_redacted)
        return res

    @staticmethod
    def shlex_join(argv: tuple[str, ...]) -> str:
        return shlex.join(argv)
