"""Subprocess wrapper for user-selected dbt executable. No dbt Python dependency."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dbt_refmerge.config import is_secret_name
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


def parse_dbt_version_output(text: str) -> str:
    """dbt-core version from ``dbt --version``; falls back to the first non-empty line.

    dbt >= 1.5 prints ``Core:`` on its own line followed by ``- installed: X``; older
    releases print ``Core: X``.
    """
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for index, line in enumerate(lines):
        if not line.lower().startswith("core:"):
            continue
        inline = line.split(":", 1)[1].split()
        if inline:
            return inline[0]
        for following in lines[index + 1 :]:
            if not following.startswith("-"):
                break
            key, _, value = following.lstrip("- ").partition(":")
            if key.strip().lower() == "installed" and value.split():
                return value.split()[0]
        break
    return lines[0] if lines else ""


def _redact_argv(argv: list[str]) -> tuple[str, ...]:
    """Mask values of secret-looking flags, and ``--vars`` payloads that mention a secret."""
    out: list[str] = []
    pending: str | None = None  # "secret" or "vars": how to treat the next token
    for tok in argv:
        if pending is not None:
            out.append("***" if pending == "secret" or is_secret_name(tok) else tok)
            pending = None
            continue
        if tok.startswith("-"):
            name, has_value, value = tok.partition("=")
            flag = name.lstrip("-")
            kind = "secret" if is_secret_name(flag) else "vars" if flag == "vars" else None
            if kind is not None:
                if not has_value:
                    pending = kind
                    out.append(tok)
                else:
                    out.append(f"{name}=***" if kind == "secret" or is_secret_name(value) else tok)
                continue
        out.append(tok)
    return tuple(out)


def _terminate(proc: subprocess.Popen[str]) -> None:
    """SIGTERM the process group; terminate() on Windows (no process groups)."""
    try:
        if sys.platform == "win32":  # pragma: win32 cover
            proc.terminate()
        else:  # pragma: win32 no cover
            os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass


def _kill(proc: subprocess.Popen[str]) -> None:
    """SIGKILL the process group; kill() on Windows."""
    try:
        if sys.platform == "win32":  # pragma: win32 cover
            proc.kill()
        else:  # pragma: win32 no cover
            os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _retained(text: str) -> str:
    """The last MAX_RETAINED_LOG_BYTES bytes of ``text`` (a split character at the cut is dropped)."""
    data = text.encode("utf-8")
    if len(data) <= MAX_RETAINED_LOG_BYTES:
        return text
    return data[-MAX_RETAINED_LOG_BYTES:].decode("utf-8", "ignore")


class DbtCli:
    def __init__(
        self,
        command: tuple[str, ...],
        capabilities: DbtCliCapabilities | None = None,
        *,
        terminate_grace_seconds: float = 10.0,
    ) -> None:
        if not command:
            raise DbtError("empty dbt command", argv=())
        self._command = tuple(command)
        self._capabilities = capabilities or DbtCliCapabilities()
        self._terminate_grace_seconds = terminate_grace_seconds

    def _stop(self, proc: subprocess.Popen[str]) -> tuple[str, str]:
        """SIGTERM, then SIGKILL after the grace period; always reap the child and drain its pipes."""
        _terminate(proc)
        try:
            return proc.communicate(timeout=self._terminate_grace_seconds)
        except subprocess.TimeoutExpired:
            _kill(proc)
            return proc.communicate()

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
                # dbt output is not guaranteed to be valid in the locale encoding (cp1252 on Windows).
                encoding="utf-8",
                errors="replace",
                shell=False,
                start_new_session=True,
            )
        except OSError as exc:
            raise DbtError(f"failed to launch dbt: {exc}", argv=_redact_argv(argv)) from exc
        timed_out = False
        try:
            stdout, stderr = proc.communicate(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            stdout, stderr = self._stop(proc)
        except KeyboardInterrupt:
            self._stop(proc)
            raise
        duration = time.monotonic() - start
        return CommandResult(
            argv_redacted=_redact_argv(argv),
            returncode=proc.returncode,
            stdout=_retained(stdout),
            stderr=_retained(stderr),
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
        return DbtVersion(raw=text, version=parse_dbt_version_output(text))

    def discover_capabilities(self, cwd: Path | None = None) -> DbtCliCapabilities:
        res = self._run_argv(
            [*self._command, "compile", "--help"],
            cwd=cwd or Path.cwd(),
            env=None,
            timeout_seconds=120,
        )
        compile_help = (res.stdout + res.stderr).lower()
        caps = DbtCliCapabilities(
            supports_no_partial_parse="no-partial-parse" in compile_help,
            supports_no_populate_cache="no-populate-cache" in compile_help,
            supports_no_introspect="no-introspect" in compile_help or "introspect" in compile_help,
            supports_target_path="target-path" in compile_help,
        )
        self._capabilities = caps
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
        if invocation.vars_json:
            argv += ["--vars", invocation.vars_json]
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
        if invocation.vars_json:
            argv += ["--vars", invocation.vars_json]
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
        if invocation.vars_json:
            argv += ["--vars", invocation.vars_json]
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
