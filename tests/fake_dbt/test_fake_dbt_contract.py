"""The fake dbt must produce artifacts the real loaders accept; every fake_dbt test relies on it."""

from pathlib import Path

import pytest

from dbt_refmerge.artifacts import load_manifest
from dbt_refmerge.dbt_cli import DbtCli, DbtInvocation

pytestmark = pytest.mark.fake_dbt

MODELS = {
    "models/stg.sql": "select 1 as id, 2 as amount\n",
    "models/m.sql": "{{ config(materialized='table') }}\nwith a as (select id from {{ ref('stg') }})\nselect * from a\n",
}


def _invocation(root: Path, target: Path) -> DbtInvocation:
    return DbtInvocation(project_dir=root, profiles_dir=None, profile=None, target=None, target_path=target, threads=1)


def test_fake_compile_writes_loadable_manifest(make_project, fake_dbt, tmp_path):
    root = make_project(MODELS)
    dbt = DbtCli(fake_dbt.command)

    result = dbt.compile(_invocation(root, tmp_path / "target"), "fqn:*")

    assert result.returncode == 0, result.stderr
    view = load_manifest(tmp_path / "target" / "manifest.json")
    assert view.metadata.adapter_type == "postgres"
    m = view.get("model.p.m")
    assert m is not None
    assert m.compiled_code == 'with a as (select id from "db"."sch"."stg")\nselect * from a\n'
    assert m.depends_on.nodes == ["model.p.stg"]
    assert m.config["materialized"] == "table"
    assert m.original_file_path == "models/m.sql"


def test_fake_compile_only_selected_nodes_get_compiled_code(make_project, fake_dbt, tmp_path):
    root = make_project(MODELS)

    DbtCli(fake_dbt.command).compile(_invocation(root, tmp_path / "target"), "fqn:stg")

    view = load_manifest(tmp_path / "target" / "manifest.json")
    assert view.get("model.p.stg").compiled_code == "select 1 as id, 2 as amount\n"
    assert view.get("model.p.m").compiled_code is None
    assert fake_dbt.calls()[-1][:3] == ["compile", "--project-dir", str(root)]
