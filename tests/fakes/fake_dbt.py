"""Fake dbt executable for the fake_dbt test lane.

Run as ``(sys.executable, <this file>, *argv)``. It understands just enough of
the dbt CLI for dbt-refmerge: ``--version``, ``--help``, ``compile --help``,
``compile`` and ``parse`` (render ``{{ ref('x') }}`` / ``{{ source('s', 't') }}``
to quoted three-part names and write a v12 ``manifest.json``), ``run``, and the
verification harness's ``run-operation`` macros, whose query results are scripted.

Behaviour switches (``FAKE_DBT_MODE``, comma separated, ``key`` or ``key=value``):

- ``version_fail``: ``--version`` exits 1
- ``compile_fail`` / ``candidate_compile_fail``: baseline / candidate compile exits 1
- ``ignore_target_path``: write the manifest to ``<project>/target`` instead
- ``adapter_type=<name>``: manifest ``metadata.adapter_type`` (default ``postgres``)
- ``candidate_drop_node``: candidate manifest omits the selected model
- ``candidate_bad_manifest``: candidate manifest is not valid JSON
- ``original_file_path_prefix=<p>``: prefix every node's ``original_file_path`` (a file dbt-refmerge cannot find)
- ``candidate_drift``: candidate compiled SQL gains ``limit 1``
- ``sleep=<seconds>``, ``ignore_sigterm``, ``sigint_parent=<delay>``: process-control tests
- ``big_logs=<chars>`` (with ``big_logs_char=<c>``): write that many characters to stdout and stderr
- ``invalid_utf8``: write bytes that are not UTF-8 to stdout
- ``echo_env=<NAME>``: print ``NAME=<value>`` to stdout
- ``exit=<code>``: exit with ``code`` after everything else
- ``parse_fail`` / ``run_fail`` / ``drop_fail``: that harness step exits 1
- ``query_fail=<label>``: the ``dbt_refmerge_query`` call with that label exits 1
- ``harness_materialized=<m>``, ``harness_schema=<s>``, ``harness_extra_node``: preflight violations

``FAKE_DBT_VERSION_OUTPUT`` / ``FAKE_DBT_COMPILE_HELP`` replace those outputs, and
``FAKE_DBT_ARGV_LOG`` appends each argv as a JSON line, and ``FAKE_DBT_PID_FILE`` receives the process id.

Harness query results (``run-operation dbt_refmerge_query``), keyed by the call's ``label``:

- ``schema``: both harness views with the columns in ``FAKE_DBT_BASELINE_COLUMNS`` /
  ``FAKE_DBT_CANDIDATE_COLUMNS`` (JSON ``[[name, type], ...]``, default ``[["id", "integer"]]``)
- ``verdict``: ``FAKE_DBT_VERDICT`` = ``"baseline,candidate,baseline_only,candidate_only"`` (default ``1,1,0,0``)
- ``run-views``: the names in ``FAKE_DBT_RUN_VIEWS`` (comma separated)
- ``FAKE_DBT_QUERY_OUTPUT`` replaces the marked output entirely (for malformed-result tests)

``run-operation dbt_refmerge_drop_views`` reports no remaining relations, or every requested name when
``FAKE_DBT_REMAINING=all``.
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


ALIAS_RE = re.compile(r"""alias\s*=\s*['"]([A-Za-z0-9_]+)['"]""")
HARNESS_NAME_RE = re.compile(r"'(dbt_refmerge_(?:baseline|candidate)_[a-z0-9_]+)'")


def _selected(selector: str | None, name: str, path: str = "") -> bool:
    if selector in (None, "fqn:*", "*"):
        return True
    return selector in (name, f"fqn:{name}", f"path:{path}")


def _compile(args: list[str], modes: dict[str, str], *, parse_only: bool = False) -> int:
    project = Path(_opt(args, "--project-dir") or ".")
    is_candidate = project.name == "candidate_project"
    if ("compile_fail" in modes and not is_candidate) or ("candidate_compile_fail" in modes and is_candidate):
        print("fake dbt: compilation error", file=sys.stderr)
        return 1
    if parse_only and "parse_fail" in modes:
        print("fake dbt: parse error", file=sys.stderr)
        return 1
    package = _project_name(project)
    dbt_vars = json.loads(_opt(args, "--vars") or "{}")
    schema = modes.get("harness_schema") or dbt_vars.get("dbt_refmerge_scratch_schema", "sch")
    selector = _opt(args, "--select")
    target_path = _opt(args, "--target-path")
    target = project / "target" if target_path is None or "ignore_target_path" in modes else Path(target_path)
    names = {p.stem for p in (project / "models").rglob("*.sql")}
    nodes: dict[str, object] = {}
    sources: dict[str, object] = {}
    for path in sorted((project / "models").rglob("*.sql")):
        name = path.stem
        uid = f"model.{package}.{name}"
        if (
            is_candidate
            and "candidate_drop_node" in modes
            and _selected(selector, name, path.relative_to(project).as_posix())
        ):
            continue
        raw = path.read_text(encoding="utf-8")
        config_match = CONFIG_RE.search(raw)
        materialized = "view"
        alias = name
        if config_match:
            mat = MATERIALIZED_RE.search(config_match.group(1))
            materialized = mat.group(1) if mat else "view"
            alias_match = ALIAS_RE.search(config_match.group(1))
            alias = alias_match.group(1) if alias_match else name
        if parse_only and "harness_materialized" in modes:
            materialized = modes["harness_materialized"]
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
        if (
            is_candidate
            and "candidate_drift" in modes
            and _selected(selector, name, path.relative_to(project).as_posix())
        ):
            compiled = compiled.rstrip() + " limit 1\n"
        nodes[uid] = {
            "unique_id": uid,
            "resource_type": "model",
            "package_name": package,
            "name": name,
            "path": path.relative_to(project / "models").as_posix(),
            "original_file_path": modes.get("original_file_path_prefix", "") + path.relative_to(project).as_posix(),
            "database": "db",
            "schema": schema,
            "alias": alias,
            "relation_name": f'"db"."{schema}"."{alias}"',
            "raw_code": raw,
            "compiled_code": compiled
            if _selected(selector, name, path.relative_to(project).as_posix()) and not parse_only
            else None,
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
    if parse_only and "harness_extra_node" in modes:
        nodes[f"seed.{package}.extra"] = {
            "unique_id": f"seed.{package}.extra",
            "resource_type": "seed",
            "package_name": package,
            "name": "extra",
            "original_file_path": "seeds/extra.csv",
            "config": {"enabled": True},
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
    if is_candidate and "candidate_bad_manifest" in modes:
        (target / "manifest.json").write_text("{not json", encoding="utf-8")
        return 0
    (target / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    return 0


def _columns(env_name: str) -> list[list[str]]:
    columns: list[list[str]] = json.loads(os.environ.get(env_name, '[["id", "integer"]]'))
    return columns


def _query_rows(label: str, sql: str) -> tuple[list[str], list[list[str | None]]]:
    names = HARNESS_NAME_RE.findall(sql)
    if label == "schema":
        rows: list[list[str | None]] = []
        for relname in sorted(names):
            role = "BASELINE" if "_baseline_" in relname else "CANDIDATE"
            for ordinal, (column, data_type) in enumerate(_columns(f"FAKE_DBT_{role}_COLUMNS"), start=1):
                rows.append([relname, "v", str(ordinal), column, data_type])
        return ["relname", "relkind", "attnum", "attname", "format_type"], rows
    if label == "verdict":
        counts = os.environ.get("FAKE_DBT_VERDICT", "1,1,0,0").split(",")
        return ["baseline_rows", "candidate_rows", "baseline_only_occurrences", "candidate_only_occurrences"], [counts]
    if label == "run-views":
        views = [v for v in os.environ.get("FAKE_DBT_RUN_VIEWS", "").split(",") if v]
        return ["relname"], [[v] for v in views]
    raise SystemExit(f"fake dbt: no scripted result for query label {label!r}")


def _print_result(nonce: str, columns: list[str], rows: list[list[str | None]]) -> None:
    print(f"DBT_REFMERGE_RESULT_{nonce}_BEGIN")
    print(json.dumps({"columns": columns, "rows": rows}))
    print(f"DBT_REFMERGE_RESULT_{nonce}_END")


def _run_operation(args: list[str], modes: dict[str, str]) -> int:
    macro = args[1]
    macro_args = json.loads(_opt(args, "--args") or "{}")
    if macro == "dbt_refmerge_drop_views":
        if "drop_fail" in modes:
            return 1
        remaining = macro_args["identifiers"] if os.environ.get("FAKE_DBT_REMAINING") == "all" else []
        _print_result(macro_args["nonce"], ["relname"], [[name] for name in remaining])
        return 0
    if macro != "dbt_refmerge_query":
        print(f"fake dbt: unknown macro {macro}", file=sys.stderr)
        return 1
    label = macro_args["label"]
    if modes.get("query_fail") == label:
        print(f"fake dbt: database error in {label}", file=sys.stderr)
        return 1
    nonce = macro_args["nonce"]
    if "FAKE_DBT_QUERY_OUTPUT" in os.environ:
        sys.stdout.write(os.environ["FAKE_DBT_QUERY_OUTPUT"].replace("{nonce}", nonce))
        return 0
    columns, rows = _query_rows(label, macro_args["sql"])
    _print_result(nonce, columns, rows)
    return 0


def main(args: list[str]) -> int:
    modes = _modes()
    log = os.environ.get("FAKE_DBT_ARGV_LOG")
    if log:
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(args) + "\n")
    pid_file = os.environ.get("FAKE_DBT_PID_FILE")
    if pid_file:
        with open(pid_file, "w", encoding="utf-8") as fh:
            fh.write(str(os.getpid()))
    if "ignore_sigterm" in modes:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    if "sigint_parent" in modes:
        time.sleep(float(modes["sigint_parent"] or 0))
        os.kill(os.getppid(), signal.SIGINT)
    if "sleep" in modes:
        time.sleep(float(modes["sleep"]))
    if "big_logs" in modes:
        count = int(modes["big_logs"])
        char = modes.get("big_logs_char", "o")
        sys.stdout.buffer.write((char * count).encode("utf-8"))
        sys.stderr.buffer.write((char * count).encode("utf-8"))
        sys.stdout.flush()
        sys.stderr.flush()
    if "invalid_utf8" in modes:
        sys.stdout.buffer.write(b"before \xff\xfe after\n")
        sys.stdout.flush()
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
    elif args and args[0] == "parse":
        code = _compile(args, modes, parse_only=True)
    elif args and args[0] == "run":
        code = 1 if "run_fail" in modes else 0
    elif args and args[0] == "run-operation":
        code = _run_operation(args, modes)
    else:
        print(f"fake dbt: unsupported arguments {args!r}", file=sys.stderr)
        code = 2
    if "exit" in modes:
        code = int(modes["exit"])
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
