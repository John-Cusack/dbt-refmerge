"""Fake dbt executable for the fake_dbt test lane.

Run as ``(sys.executable, <this file>, *argv)``. It understands just enough of
the dbt CLI for dbt-refmerge: ``--version``, ``--help``, ``compile --help`` and
``compile``. ``compile`` renders ``{{ ref('x') }}`` and ``{{ source('s', 't') }}``
to quoted three-part names and writes a v12 ``manifest.json``.

Behaviour switches (``FAKE_DBT_MODE``, comma separated, ``key`` or ``key=value``):

- ``version_fail``: ``--version`` exits 1
- ``compile_fail`` / ``candidate_compile_fail``: baseline / candidate compile exits 1
- ``ignore_target_path``: write the manifest to ``<project>/target`` instead
- ``adapter_type=<name>``: manifest ``metadata.adapter_type`` (default ``postgres``)
- ``candidate_drop_node``: candidate manifest omits the selected model
- ``candidate_drift``: candidate compiled SQL gains ``limit 1``
- ``sleep=<seconds>``, ``ignore_sigterm``, ``sigint_parent``: process-control tests
- ``big_logs=<chars>``: write that many characters to stdout and stderr
- ``echo_env=<NAME>``: print ``NAME=<value>`` to stdout
- ``exit=<code>``: exit with ``code`` after everything else

``FAKE_DBT_VERSION_OUTPUT`` / ``FAKE_DBT_COMPILE_HELP`` replace those outputs, and
``FAKE_DBT_ARGV_LOG`` appends each argv as a JSON line.
"""

from __future__ import annotations

import json
import os
import re
import signal
import sys
import time
from pathlib import Path

VERSION_OUTPUT = (
    "Core:\n  - installed: 1.9.0\n  - latest:    1.9.0 - Up to date!\n\nPlugins:\n  - postgres: 1.9.0 - Up to date!\n"
)
COMPILE_HELP = (
    "Usage: dbt compile [OPTIONS]\n"
    "  --partial-parse / --no-partial-parse\n"
    "  --populate-cache / --no-populate-cache\n"
    "  --introspect / --no-introspect\n"
    "  --target-path TEXT\n"
    "  -s, --select TEXT\n"
    "  --threads INTEGER\n"
)
REF_RE = re.compile(r"""\{\{\s*ref\(\s*['"]([A-Za-z0-9_]+)['"]\s*\)\s*\}\}""")
SOURCE_RE = re.compile(r"""\{\{\s*source\(\s*['"]([A-Za-z0-9_]+)['"]\s*,\s*['"]([A-Za-z0-9_]+)['"]\s*\)\s*\}\}""")
CONFIG_RE = re.compile(r"""\{\{\s*config\((.*?)\)\s*\}\}\s*""", re.S)
MATERIALIZED_RE = re.compile(r"""materialized\s*=\s*['"]([a-z_]+)['"]""")


def _modes() -> dict[str, str]:
    out: dict[str, str] = {}
    for item in os.environ.get("FAKE_DBT_MODE", "").split(","):
        if item.strip():
            key, _, value = item.strip().partition("=")
            out[key] = value
    return out


def _opt(args: list[str], name: str) -> str | None:
    return args[args.index(name) + 1] if name in args else None


def _project_name(project: Path) -> str:
    for filename in ("dbt_project.yml", "dbt_project.yaml"):
        path = project / filename
        if path.is_file():
            match = re.search(r"^name:\s*['\"]?([A-Za-z0-9_]+)", path.read_text(encoding="utf-8"), re.M)
            if match:
                return match.group(1)
    return "project"


def _selected(selector: str | None, name: str) -> bool:
    if selector in (None, "fqn:*", "*"):
        return True
    return selector in (name, f"fqn:{name}")


def _compile(args: list[str], modes: dict[str, str]) -> int:
    project = Path(_opt(args, "--project-dir") or ".")
    is_candidate = project.name == "candidate_project"
    if ("compile_fail" in modes and not is_candidate) or ("candidate_compile_fail" in modes and is_candidate):
        print("fake dbt: compilation error", file=sys.stderr)
        return 1
    package = _project_name(project)
    selector = _opt(args, "--select")
    target_path = _opt(args, "--target-path")
    target = project / "target" if target_path is None or "ignore_target_path" in modes else Path(target_path)
    names = {p.stem for p in (project / "models").rglob("*.sql")}
    nodes: dict[str, object] = {}
    sources: dict[str, object] = {}
    for path in sorted((project / "models").rglob("*.sql")):
        name = path.stem
        uid = f"model.{package}.{name}"
        if is_candidate and "candidate_drop_node" in modes and _selected(selector, name):
            continue
        raw = path.read_text(encoding="utf-8")
        config_match = CONFIG_RE.search(raw)
        materialized = "view"
        if config_match:
            mat = MATERIALIZED_RE.search(config_match.group(1))
            materialized = mat.group(1) if mat else "view"
        refs = REF_RE.findall(raw)
        missing = sorted(set(refs) - names)
        if missing:
            print(f"fake dbt: {uid} depends on a node named '{missing[0]}' which was not found", file=sys.stderr)
            return 1
        source_pairs = SOURCE_RE.findall(raw)
        for source_name, table in source_pairs:
            sources[f"source.{package}.{source_name}.{table}"] = {
                "unique_id": f"source.{package}.{source_name}.{table}",
                "resource_type": "source",
                "package_name": package,
                "source_name": source_name,
                "name": table,
                "database": "db",
                "schema": source_name,
                "identifier": table,
            }
        compiled = CONFIG_RE.sub("", raw)
        compiled = REF_RE.sub(lambda m: f'"db"."sch"."{m.group(1)}"', compiled)
        compiled = SOURCE_RE.sub(lambda m: f'"db"."{m.group(1)}"."{m.group(2)}"', compiled)
        if is_candidate and "candidate_drift" in modes and _selected(selector, name):
            compiled = compiled.rstrip() + " limit 1\n"
        nodes[uid] = {
            "unique_id": uid,
            "resource_type": "model",
            "package_name": package,
            "name": name,
            "path": path.relative_to(project / "models").as_posix(),
            "original_file_path": path.relative_to(project).as_posix(),
            "database": "db",
            "schema": "sch",
            "alias": name,
            "relation_name": f'"db"."sch"."{name}"',
            "raw_code": raw,
            "compiled_code": compiled if _selected(selector, name) else None,
            "depends_on": {
                "macros": [],
                "nodes": sorted(
                    {f"model.{package}.{r}" for r in refs} | {f"source.{package}.{s}.{t}" for s, t in source_pairs}
                ),
            },
            "refs": [{"name": r, "package": None, "version": None} for r in refs],
            "sources": [[s, t] for s, t in source_pairs],
            "config": {"materialized": materialized, "enabled": True},
        }
    target.mkdir(parents=True, exist_ok=True)
    manifest = {
        "metadata": {
            "dbt_schema_version": "https://schemas.getdbt.com/dbt/manifest/v12.json",
            "dbt_version": "1.9.0",
            "adapter_type": modes.get("adapter_type", "postgres"),
            "project_name": package,
        },
        "nodes": nodes,
        "sources": sources,
    }
    (target / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    return 0


def main(args: list[str]) -> int:
    modes = _modes()
    log = os.environ.get("FAKE_DBT_ARGV_LOG")
    if log:
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(args) + "\n")
    if "ignore_sigterm" in modes:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    if "sigint_parent" in modes:
        os.kill(os.getppid(), signal.SIGINT)
    if "sleep" in modes:
        time.sleep(float(modes["sleep"]))
    if "big_logs" in modes:
        count = int(modes["big_logs"])
        sys.stdout.write("o" * count)
        sys.stderr.write("e" * count)
    if "echo_env" in modes:
        name = modes["echo_env"]
        print(f"{name}={os.environ.get(name, '')}")
    code = 0
    if args == ["--version"]:
        if "version_fail" in modes:
            code = 1
        else:
            sys.stdout.write(os.environ.get("FAKE_DBT_VERSION_OUTPUT", VERSION_OUTPUT))
    elif args == ["--help"]:
        print("Usage: dbt [OPTIONS] COMMAND [ARGS]...")
    elif args == ["compile", "--help"]:
        sys.stdout.write(os.environ.get("FAKE_DBT_COMPILE_HELP", COMPILE_HELP))
    elif args and args[0] == "compile":
        code = _compile(args, modes)
    else:
        print(f"fake dbt: unsupported arguments {args!r}", file=sys.stderr)
        code = 2
    if "exit" in modes:
        code = int(modes["exit"])
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
