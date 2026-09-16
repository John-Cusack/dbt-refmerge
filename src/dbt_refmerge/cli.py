"""Typer CLI: scan / check / fix / cleanup. No business logic beyond parsing/rendering.

Every option defaults to None (or is omitted from the overrides) so that an unset flag never replaces a
value from ``.dbt-refmerge.toml`` or ``DBT_REFMERGE_*``.
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import NoReturn, TypeVar

import typer
from rich.console import Console

from dbt_refmerge import __version__
from dbt_refmerge.config import AppConfig, FailOn, load_config
from dbt_refmerge.domain import VerificationStatus, is_fixable
from dbt_refmerge.orchestrator import (
    CheckRequest,
    CleanupRequest,
    FixRequest,
    RefmergeService,
    ScanRequest,
)
from dbt_refmerge.reporting import (
    ExitCode,
    OutputFormat,
    check_report_json,
    evaluate_exit_code_for_check,
    render_github_scan,
    render_human_check,
    render_human_scan,
    scan_report_json,
)

app = typer.Typer(add_completion=False, help="Safe merge of duplicate direct-import CTEs.")
# Plain text: paths such as models/[legacy]/m.sql are not markup, and lines must not wrap.
console = Console(markup=False, highlight=False, soft_wrap=True)
err_console = Console(stderr=True, markup=False, highlight=False, soft_wrap=True)

T = TypeVar("T")


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


def _load(project_dir: Path, **overrides: object) -> AppConfig:
    try:
        return load_config(project_dir, cli_overrides=overrides)
    except Exception as exc:
        _fail("configuration error", exc, debug=bool(overrides.get("debug")))


def _run(action: str, config: AppConfig, call: Callable[[], T]) -> T:
    try:
        return call()
    except KeyboardInterrupt:
        err_console.print("interrupted")
        raise typer.Exit(code=int(ExitCode.INTERRUPTED)) from None
    except Exception as exc:
        _fail(f"{action} failed", exc, debug=config.debug)


def _service(config: AppConfig) -> RefmergeService:
    """In human mode, report each stage on stderr; JSON mode leaves stderr to errors."""
    if config.json_output:
        return RefmergeService()
    return RefmergeService(progress=lambda message: err_console.print(f"dbt-refmerge: {message}"))


def _fail(prefix: str, exc: BaseException, *, debug: bool) -> NoReturn:
    if debug:
        err_console.print("".join(traceback.format_exception(exc)).rstrip())
    err_console.print(f"{prefix}: {exc}")
    raise typer.Exit(code=int(ExitCode.OPERATIONAL_ERROR))


def _command(parts: list[str] | None) -> tuple[str, ...] | None:
    return tuple(parts) if parts else None


def _write_json(payload: object) -> None:
    sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")


@app.command()
def scan(
    project_dir: Path = typer.Option(Path("."), "--project-dir"),
    profiles_dir: Path | None = typer.Option(None, "--profiles-dir"),
    profile: str | None = typer.Option(None, "--profile"),
    target: str | None = typer.Option(None, "--target"),
    adapter: str | None = typer.Option(None, "--adapter"),
    format_: OutputFormat | None = typer.Option(
        None, "--format", help="text, json, or github (GitHub Actions annotations)."
    ),
    json_: bool | None = typer.Option(None, "--json", show_default=False, help="Same as --format json."),
    fail_on: FailOn | None = typer.Option(None, "--fail-on", help="finding: exit 2 when there are leads."),
    debug: bool | None = typer.Option(None, "--debug", show_default=False),
) -> None:
    """List duplicate import CTEs from source files (no dbt run, no warehouse)."""
    if json_ and format_ not in (None, OutputFormat.JSON):
        _fail("configuration error", ValueError(f"--json conflicts with --format {format_.value}"), debug=False)
    config = _load(
        project_dir,
        profiles_dir=profiles_dir,
        profile=profile,
        target=target,
        adapter=adapter,
        json_output=json_ if format_ is None else format_ is OutputFormat.JSON,
        fail_on=fail_on,
        debug=debug,
    )
    report = _run("scan", config, lambda: RefmergeService().scan(ScanRequest(config=config)))
    if config.json_output:
        _write_json(scan_report_json(report, project_dir=config.project_dir))
    elif format_ is OutputFormat.GITHUB:
        # Annotations name files relative to the checkout, which is the working directory in a workflow.
        workspace = Path(os.environ.get("GITHUB_WORKSPACE") or Path.cwd())
        sys.stdout.write(render_github_scan(report, base_dir=workspace))
    else:
        sys.stdout.write(render_human_scan(report, project_dir=config.project_dir))
    failed = config.fail_on is FailOn.FINDING and report.findings
    raise typer.Exit(code=int(ExitCode.POLICY_FINDING) if failed else 0)


@app.command()
def check(
    project_dir: Path = typer.Option(Path("."), "--project-dir"),
    profiles_dir: Path | None = typer.Option(None, "--profiles-dir"),
    profile: str | None = typer.Option(None, "--profile"),
    target: str | None = typer.Option(None, "--target"),
    dbt_command_part: list[str] | None = typer.Option(None, "--dbt-command-part"),
    select: str | None = typer.Option(None, "--select", help="dbt selector (default: every model)."),
    scratch_schema: str | None = typer.Option(None, "--scratch-schema"),
    adapter: str | None = typer.Option(None, "--adapter"),
    json_: bool | None = typer.Option(None, "--json", show_default=False),
    fail_on: FailOn | None = typer.Option(None, "--fail-on"),
    keep_workspace: bool | None = typer.Option(None, "--keep-workspace", show_default=False),
    debug: bool | None = typer.Option(None, "--debug", show_default=False),
) -> None:
    """Prove each duplicate-import merge on the warehouse. Never edits files."""
    config = _load(
        project_dir,
        profiles_dir=profiles_dir,
        profile=profile,
        target=target,
        dbt_command=_command(dbt_command_part),
        scratch_schema=scratch_schema,
        adapter=adapter,
        json_output=json_,
        fail_on=fail_on,
        keep_workspace=keep_workspace,
        debug=debug,
    )
    report = _run("check", config, lambda: _service(config).check(CheckRequest(config=config, select=select)))
    if config.json_output:
        _write_json(
            check_report_json(
                report,
                command="check",
                project_dir=config.project_dir,
                dbt_version=report.dbt_version,
                manifest_schema_version=report.manifest_schema_version,
                workspace=report.workspace_root,
            )
        )
    else:
        console.print(render_human_check(report))
        if report.workspace_root is not None:
            console.print(f"workspace kept at {report.workspace_root}")
    raise typer.Exit(code=int(evaluate_exit_code_for_check(report, config.fail_on)))


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
    dry_run: bool = typer.Option(False, "--dry-run", help="Prove and show the diff without writing."),
    json_: bool | None = typer.Option(None, "--json", show_default=False),
    keep_workspace: bool | None = typer.Option(None, "--keep-workspace", show_default=False),
    debug: bool | None = typer.Option(None, "--debug", show_default=False),
) -> None:
    """Re-prove one model's merge and write it only if the proof passes."""
    config = _load(
        project_dir,
        profiles_dir=profiles_dir,
        profile=profile,
        target=target,
        dbt_command=_command(dbt_command_part),
        scratch_schema=scratch_schema,
        adapter=adapter,
        json_output=json_,
        keep_workspace=keep_workspace,
        debug=debug,
    )
    request = FixRequest(config=config, model_path=model_path, dry_run=dry_run)
    report = _run("fix", config, lambda: _service(config).fix(request))
    result = report.result
    if result is None:
        err_console.print(f"fix: {report.reason}")
        raise typer.Exit(code=int(ExitCode.OPERATIONAL_ERROR))
    receipt = result.receipt
    if config.json_output:
        _write_json(
            {
                "applied": report.applied,
                "dry_run": report.dry_run,
                "reason": report.reason,
                "model_unique_id": receipt.model_unique_id,
                "status": receipt.status.value,
                "reason_codes": [code.value for code in receipt.reason_codes],
                "diff": result.diff,
            }
        )
    else:
        if report.dry_run and result.diff:
            console.print(result.diff.rstrip("\n"))
        console.print(f"applied={report.applied} dry_run={report.dry_run} {report.reason}")
    if report.applied or (report.dry_run and is_fixable(receipt)):
        raise typer.Exit(code=0)
    code = ExitCode.DIFFERENT if receipt.status is VerificationStatus.DIFFERENT else ExitCode.UNVERIFIABLE
    raise typer.Exit(code=int(code))


@app.command()
def cleanup(
    run_id: str = typer.Option(..., "--run-id"),
    project_dir: Path = typer.Option(Path("."), "--project-dir"),
    profiles_dir: Path | None = typer.Option(None, "--profiles-dir"),
    profile: str | None = typer.Option(None, "--profile"),
    target: str | None = typer.Option(None, "--target"),
    dbt_command_part: list[str] | None = typer.Option(None, "--dbt-command-part"),
    scratch_schema: str | None = typer.Option(None, "--scratch-schema"),
    debug: bool | None = typer.Option(None, "--debug", show_default=False),
) -> None:
    """Drop the scratch views an interrupted check left behind (found by run id in the scratch schema)."""
    config = _load(
        project_dir,
        profiles_dir=profiles_dir,
        profile=profile,
        target=target,
        dbt_command=_command(dbt_command_part),
        scratch_schema=scratch_schema,
        debug=debug,
    )
    request = CleanupRequest(config=config, run_id=run_id)
    result = _run("cleanup", config, lambda: RefmergeService().cleanup(request))
    _write_json(result)
    raise typer.Exit(code=0 if result["complete"] else int(ExitCode.OPERATIONAL_ERROR))


if __name__ == "__main__":
    app()
