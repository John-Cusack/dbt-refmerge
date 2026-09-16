"""Orchestrator pieces that need no dbt: model discovery and scan mapping."""

import json

from dbt_refmerge.config import AppConfig
from dbt_refmerge.orchestrator import RefmergeService, ScanRequest, discover_model_files

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
