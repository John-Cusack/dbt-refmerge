"""Typer CLI: scan / check / fix / cleanup. No business logic beyond parsing/rendering."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import typer
from rich.console import Console

from dbt_refmerge import __version__
from dbt_refmerge.config import AppConfig, FailOn, load_config
from dbt_refmerge.orchestrator import (
    CheckRequest,
    CleanupRequest,
    FixRequest,
    RefmergeService,
    ScanRequest,
)
from dbt_refmerge.reporting import (
    ExitCode,
    check_report_json,
    evaluate_exit_code_for_check,
    render_human_check,
    scan_report_json,
)

app = typer.Typer(add_completion=False, help="Safe merge of duplicate direct-import CTEs.")
console = Console()
err_console = Console(stderr=True)


def _version_callback(value: bool) -> None:
    if value:
        sys.stdout.write(f"dbt-refmerge {__version__}\n")
        raise typer.Exit(code=0)


@app.callback()
def main(
    version: bool = typer.Option(
        False, "--version", callback=_version_callback, is_eager=True, help="Show the version and exit."
    ),
) -> None:
    pass


def _common_config(
    project_dir: Path,
    profiles_dir: Path | None,
    profile: str | None,
    target: str | None,
    dbt_command_part: list[str] | None,
    scratch_schema: str | None,
    adapter: str | None,
    fail_on: FailOn,
    json_output: bool,
    debug: bool,
    keep_workspace: bool,
    allow_compile_introspection: bool,
) -> AppConfig:
    overrides: dict[str, object] = {}
    if profiles_dir is not None:
        overrides["profiles_dir"] = profiles_dir
    if profile is not None:
        overrides["profile"] = profile
    if target is not None:
        overrides["target"] = target
    if dbt_command_part:
        overrides["dbt_command"] = tuple(dbt_command_part)
    if scratch_schema is not None:
        overrides["scratch_schema"] = scratch_schema
    if adapter is not None:
        overrides["adapter"] = adapter
    overrides["fail_on"] = fail_on
    overrides["json_output"] = json_output
    overrides["debug"] = debug
    overrides["keep_workspace"] = keep_workspace
    overrides["allow_compile_introspection"] = allow_compile_introspection
    return load_config(project_dir, cli_overrides=overrides)


@app.command()
def scan(
    project_dir: Path = typer.Option(Path("."), "--project-dir"),
    profiles_dir: Path | None = typer.Option(None, "--profiles-dir"),
    profile: str | None = typer.Option(None, "--profile"),
    target: str | None = typer.Option(None, "--target"),
    dbt_command_part: list[str] | None = typer.Option(None, "--dbt-command-part"),
    adapter: str | None = typer.Option(None, "--adapter"),
    select: str | None = typer.Option(None, "--select"),
    compile_: bool = typer.Option(False, "--compile"),
    json_: bool = typer.Option(False, "--json"),
    fail_on: FailOn = typer.Option(FailOn.FIXABLE, "--fail-on"),
    debug: bool = typer.Option(False, "--debug"),
) -> None:
    try:
        config = _common_config(
            project_dir,
            profiles_dir,
            profile,
            target,
            dbt_command_part,
            None,
            adapter,
            fail_on,
            json_,
            debug,
            False,
            False,
        )
    except Exception as exc:
        err_console.print(f"configuration error: {exc}")
        raise typer.Exit(code=int(ExitCode.OPERATIONAL_ERROR)) from None
    svc = RefmergeService()
    try:
        report = svc.scan(ScanRequest(config=config, select=select, compile=compile_))
    except Exception as exc:
        err_console.print(f"scan failed: {exc}")
        raise typer.Exit(code=int(ExitCode.OPERATIONAL_ERROR)) from None
    if json_:
        sys.stdout.write(
            json.dumps(scan_report_json(report, project_dir=config.project_dir), indent=2, sort_keys=True) + "\n"
        )
    else:
        for f in report.findings:
            console.print(f"{f.source_path}: {', '.join(f.cte_names)} -> {f.upstream_unique_id or '?'}")
    raise typer.Exit(code=0)


@app.command()
def check(
    project_dir: Path = typer.Option(Path("."), "--project-dir"),
    profiles_dir: Path | None = typer.Option(None, "--profiles-dir"),
    profile: str | None = typer.Option(None, "--profile"),
    target: str | None = typer.Option(None, "--target"),
    dbt_command_part: list[str] | None = typer.Option(None, "--dbt-command-part"),
    select: str | None = typer.Option(None, "--select"),
    scratch_schema: str | None = typer.Option(None, "--scratch-schema"),
    adapter: str | None = typer.Option(None, "--adapter"),
    json_: bool = typer.Option(False, "--json"),
    fail_on: FailOn = typer.Option(FailOn.FIXABLE, "--fail-on"),
    keep_workspace: bool = typer.Option(False, "--keep-workspace"),
    allow_compile_introspection: bool = typer.Option(False, "--allow-compile-introspection"),
    debug: bool = typer.Option(False, "--debug"),
) -> None:
    try:
        config = _common_config(
            project_dir,
            profiles_dir,
            profile,
            target,
            dbt_command_part,
            scratch_schema,
            adapter,
            fail_on,
            json_,
            debug,
            keep_workspace,
            allow_compile_introspection,
        )
    except Exception as exc:
        err_console.print(f"configuration error: {exc}")
        raise typer.Exit(code=int(ExitCode.OPERATIONAL_ERROR)) from None
    svc = RefmergeService()
    try:
        report = svc.check(CheckRequest(config=config, select=select))
    except Exception as exc:
        err_console.print(f"check failed: {exc}")
        raise typer.Exit(code=int(ExitCode.OPERATIONAL_ERROR)) from None
    if json_:
        sys.stdout.write(
            json.dumps(
                check_report_json(report, command="check", project_dir=config.project_dir),
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
    else:
        console.print(render_human_check(report))
    raise typer.Exit(code=int(evaluate_exit_code_for_check(report, fail_on)))


@app.command()
def fix(
    model_path: Path = typer.Argument(...),
    project_dir: Path = typer.Option(Path("."), "--project-dir"),
    profiles_dir: Path | None = typer.Option(None, "--profiles-dir"),
    profile: str | None = typer.Option(None, "--profile"),
    target: str | None = typer.Option(None, "--target"),
    dbt_command_part: list[str] | None = typer.Option(None, "--dbt-command-part"),
    scratch_schema: str | None = typer.Option(None, "--scratch-schema"),
    adapter: str | None = typer.Option(None, "--adapter"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    json_: bool = typer.Option(False, "--json"),
    fail_on: FailOn = typer.Option(FailOn.FIXABLE, "--fail-on"),
    keep_workspace: bool = typer.Option(False, "--keep-workspace"),
    allow_compile_introspection: bool = typer.Option(False, "--allow-compile-introspection"),
    debug: bool = typer.Option(False, "--debug"),
) -> None:
    try:
        config = _common_config(
            project_dir,
            profiles_dir,
            profile,
            target,
            dbt_command_part,
            scratch_schema,
            adapter,
            fail_on,
            json_,
            debug,
            keep_workspace,
            allow_compile_introspection,
        )
    except Exception as exc:
        err_console.print(f"configuration error: {exc}")
        raise typer.Exit(code=int(ExitCode.OPERATIONAL_ERROR)) from None
    svc = RefmergeService()
    try:
        fix_report = svc.fix(FixRequest(config=config, model_path=model_path, dry_run=dry_run))
    except Exception as exc:
        err_console.print(f"fix failed: {exc}")
        raise typer.Exit(code=int(ExitCode.OPERATIONAL_ERROR)) from None
    if fix_report.result is None:
        err_console.print(f"fix: {fix_report.reason}")
        raise typer.Exit(code=int(ExitCode.OPERATIONAL_ERROR)) from None
    if json_:
        sys.stdout.write(
            json.dumps(
                {
                    "applied": fix_report.applied,
                    "dry_run": fix_report.dry_run,
                    "reason": fix_report.reason,
                },
                indent=2,
            )
            + "\n"
        )
    else:
        console.print(f"applied={fix_report.applied} dry_run={fix_report.dry_run} {fix_report.reason}")
    if fix_report.applied:
        raise typer.Exit(code=0)
    # not applied: surface policy code
    raise typer.Exit(code=int(ExitCode.UNVERIFIABLE)) from None


@app.command()
def cleanup(
    run_id: str = typer.Option(..., "--run-id"),
    project_dir: Path = typer.Option(Path("."), "--project-dir"),
    profiles_dir: Path | None = typer.Option(None, "--profiles-dir"),
    profile: str | None = typer.Option(None, "--profile"),
    target: str | None = typer.Option(None, "--target"),
    dbt_command_part: list[str] | None = typer.Option(None, "--dbt-command-part"),
    scratch_schema: str | None = typer.Option(None, "--scratch-schema"),
) -> None:
    """Drop the scratch views a check run left behind (found by run id in the scratch schema)."""
    overrides: dict[str, object] = {
        "profiles_dir": profiles_dir,
        "profile": profile,
        "target": target,
        "dbt_command": tuple(dbt_command_part) if dbt_command_part else None,
        "scratch_schema": scratch_schema,
    }
    try:
        config = load_config(project_dir, cli_overrides=overrides)
    except Exception as exc:
        err_console.print(f"configuration error: {exc}")
        raise typer.Exit(code=int(ExitCode.OPERATIONAL_ERROR)) from None
    try:
        result = RefmergeService().cleanup(CleanupRequest(config=config, run_id=run_id))
    except Exception as exc:
        err_console.print(f"cleanup failed: {exc}")
        raise typer.Exit(code=int(ExitCode.OPERATIONAL_ERROR)) from None
    sys.stdout.write(json.dumps(result, indent=2, sort_keys=True) + "\n")
    raise typer.Exit(code=0 if result["complete"] else int(ExitCode.OPERATIONAL_ERROR))


if __name__ == "__main__":
    app()
