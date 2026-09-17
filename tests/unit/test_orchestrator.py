"""Orchestrator pieces that need no dbt: model discovery and scan mapping."""

import json
from pathlib import Path

from dbt_refmerge.config import AppConfig
from dbt_refmerge.domain import ReasonCode
from dbt_refmerge.orchestrator import RefmergeService, ScanRequest, discover_model_files, project_relative_path

DUPLICATE = (
    "with a as (\n    select\n        id,\n        customer_id\n    from {{ ref('stg') }}\n),\n"
    "b as (\n    select\n        id,\n        amount\n    from {{ ref('stg') }}\n)\n"
    "select a.customer_id, b.amount from a join b using (id)\n"
)


def test_discover_model_files_uses_model_paths_relative_to_the_project(tmp_path):
    # A project living under a directory named "target" used to find nothing.
    root = tmp_path / "target" / "project"
    for rel in ("dbt/models/a.sql", "dbt/models/sub/b.sql", "models/ignored.sql", "macros/m.sql"):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text("select 1\n")
    (root / "dbt_project.yml").write_text('name: p\nmodel-paths: ["dbt/models"]\n')

    assert discover_model_files(root) == [root / "dbt/models/a.sql", root / "dbt/models/sub/b.sql"]


def test_discover_model_files_defaults_to_models_and_never_globs_the_whole_tree(tmp_path):
    (tmp_path / "dbt_project.yml").write_text("name: p\n")
    (tmp_path / "macros").mkdir()
    (tmp_path / "macros" / "m.sql").write_text("select 1\n")

    assert discover_model_files(tmp_path) == []


def test_scan_maps_files_to_manifest_nodes_by_exact_path(make_project):
    manifest = {
        "metadata": {"dbt_schema_version": "https://schemas.getdbt.com/dbt/manifest/v12.json", "dbt_version": "1.9.0"},
        "nodes": {
            "model.p.m": {
                "unique_id": "model.p.m",
                "resource_type": "model",
                "package_name": "p",
                "name": "m",
                "original_file_path": "models/m.sql",
            },
        },
    }
    # "models/am.sql" ends with "m.sql"; suffix matching used to give it model.p.m's id.
    root = make_project({"models/am.sql": DUPLICATE, "target/manifest.json": json.dumps(manifest)})

    report = RefmergeService().scan(ScanRequest(config=AppConfig(project_dir=root, adapter="postgres")))

    assert [f.model_unique_id for f in report.findings] == ["model.am"]


def _manifest_json(nodes: dict) -> str:
    return json.dumps(
        {
            "metadata": {
                "dbt_schema_version": "https://schemas.getdbt.com/dbt/manifest/v12.json",
                "dbt_version": "1.9.0",
                "project_name": "p",
            },
            "nodes": nodes,
        }
    )


def _node(name: str, deps: list[str] | None = None, package: str = "p") -> dict:
    return {
        "unique_id": f"model.{package}.{name}",
        "resource_type": "model",
        "package_name": package,
        "name": name,
        "original_file_path": f"models/{name}.sql",
        "depends_on": {"nodes": deps or []},
    }


def _scan(root):
    return RefmergeService().scan(ScanRequest(config=AppConfig(project_dir=root, adapter="postgres"))).findings


def test_scan_resolves_the_upstream_through_the_manifest(make_project):
    manifest = _manifest_json({"model.p.m": _node("m", ["model.p.stg"]), "model.p.stg": _node("stg")})
    root = make_project({"models/m.sql": DUPLICATE, "target/manifest.json": manifest})

    assert [(f.model_unique_id, f.upstream_unique_id) for f in _scan(root)] == [("model.p.m", "model.p.stg")]


def test_scan_leaves_the_upstream_blank_when_the_manifest_cannot_resolve_it(make_project):
    manifest = _manifest_json({"model.p.m": _node("m"), "model.pkg.m": _node("m", package="pkg")})
    root = make_project({"models/m.sql": DUPLICATE, "target/manifest.json": manifest})

    assert [(f.model_unique_id, f.upstream_unique_id) for f in _scan(root)] == [("model.p.m", "")]


def test_scan_ignores_an_unreadable_manifest(make_project):
    root = make_project({"models/m.sql": DUPLICATE, "target/manifest.json": "{not json"})
    assert [f.model_unique_id for f in _scan(root)] == ["model.m"]


def test_scan_reports_unparseable_models_only_when_they_name_a_relation_twice(make_project):
    # A pre-commit hook with --fail-on finding must not fail on models it merely cannot read.
    distinct = DUPLICATE.replace("ref('stg') }}\n)\nselect", "ref('other') }}\n)\nselect")
    recursive = "with recursive a as (select 1 from {{ ref('x') }})\nselect * from a join {{ ref('x') }} using (id)\n"
    root = make_project(
        {
            "models/broken.sql": "with a as (select 1 from {{ ref('x'\n",
            "models/distinct.sql": distinct,
            "models/recursive.sql": recursive,
            "models/recursive_single.sql": "with recursive a as (select 1 from {{ ref('x') }}) select 1\n",
        }
    )

    findings = _scan(root)

    assert [(f.model_unique_id, f.reason_codes, f.line) for f in findings] == [
        ("model.recursive", (ReasonCode.UNSUPPORTED_IMPORT_SHAPE,), 1)
    ]


def test_model_paths_fall_back_to_models_for_unusable_project_files(tmp_path):
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "a.sql").write_text("select 1\n")
    (tmp_path / "dbt_project.yml").write_text("model-paths: [unclosed\n")
    assert discover_model_files(tmp_path) == [tmp_path / "models" / "a.sql"]

    (tmp_path / "dbt_project.yml").unlink()
    (tmp_path / "dbt_project.yaml").write_text("model-paths: models\n")
    assert discover_model_files(tmp_path) == [tmp_path / "models" / "a.sql"]

    (tmp_path / "dbt_project.yaml").unlink()
    assert discover_model_files(tmp_path) == [tmp_path / "models" / "a.sql"]


def test_model_paths_outside_the_project_are_ignored(tmp_path):
    project = tmp_path / "project"
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside" / "x.sql").write_text("select 1\n")
    project.mkdir()
    (project / "dbt_project.yml").write_text('model-paths: ["../outside"]\n')
    assert discover_model_files(project) == []


def test_project_relative_path_forms(tmp_path, monkeypatch):
    root = tmp_path / "project"
    (root / "models").mkdir(parents=True)
    (root / "models" / "m.sql").write_text("select 1\n")
    monkeypatch.chdir(root / "models")

    assert project_relative_path(Path("m.sql"), root) == Path("models/m.sql")
    assert project_relative_path(Path("models/m.sql"), root) == Path("models/m.sql")
    assert project_relative_path(root / "models" / "m.sql", root) == Path("models/m.sql")
    assert project_relative_path(tmp_path / "elsewhere.sql", root) is None
    assert project_relative_path(Path("../../elsewhere.sql"), root) is None


def test_manifest_without_adapter_type_is_accepted():
    from dbt_refmerge.adapters import get_spec
    from dbt_refmerge.artifacts import ManifestMetadataModel, ManifestView
    from dbt_refmerge.orchestrator import _require_manifest_adapter

    metadata = ManifestMetadataModel(dbt_schema_version="v12", dbt_version="1.9.0")
    _require_manifest_adapter(
        ManifestView(metadata=metadata, nodes={}, sources={}, path=Path("m.json")), get_spec("postgres")
    )


def test_unified_diff_of_undecodable_bytes_is_empty():
    from dbt_refmerge.orchestrator import _unified_diff

    assert _unified_diff(b"\xff", b"select 1\n", "m.sql") == ""
    assert _unified_diff(b"select 1\n", b"select 2\n", "m.sql").startswith("--- a/m.sql\n+++ b/m.sql\n")
