"""Manifest loading and literal ref()/source() resolution: every refusal fails closed with ArtifactError."""

import json
from pathlib import Path

import pytest

from dbt_refmerge.artifacts import (
    MAX_ARTIFACT_BYTES,
    SUPPORTED_MANIFEST_SCHEMAS,
    DependsOnModel,
    ManifestMetadataModel,
    ManifestNodeModel,
    ManifestView,
    RefArgsModel,
    load_manifest,
    resolve_literal_ref,
)
from dbt_refmerge.domain import ReasonCode
from dbt_refmerge.errors import ArtifactError

V12 = "https://schemas.getdbt.com/dbt/manifest/v12.json"


# -- load_manifest -----------------------------------------------------------------------------------


def _node(uid: str, **fields: object) -> dict[str, object]:
    resource_type, package, name = uid.split(".")[:3]
    node: dict[str, object] = {
        "unique_id": uid,
        "resource_type": resource_type,
        "package_name": package,
        "name": name,
        "original_file_path": f"models/{name}.sql",
    }
    node.update(fields)
    return node


def _payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "metadata": {"dbt_schema_version": V12, "dbt_version": "1.9.0"},
        "nodes": {},
        "sources": {},
    }
    payload.update(overrides)
    return payload


def _write(tmp_path: Path, content: object) -> Path:
    path = tmp_path / "manifest.json"
    path.write_bytes(content if isinstance(content, bytes) else json.dumps(content).encode("utf-8"))
    return path


def test_load_manifest_nodes_models_and_get(tmp_path):
    source = {"unique_id": "source.p.raw.orders", "source_name": "raw", "name": "orders"}
    path = _write(
        tmp_path,
        _payload(
            metadata={"dbt_schema_version": V12, "dbt_version": "1.9.0", "adapter_type": "postgres"},
            nodes={
                "model.p.orders": _node("model.p.orders", depends_on={"nodes": ["source.p.raw.orders"]}),
                "seed.p.countries": _node("seed.p.countries", original_file_path="seeds/countries.csv"),
            },
            sources={"source.p.raw.orders": source},
        ),
    )

    view = load_manifest(str(path))

    assert view.path == path
    assert view.metadata.adapter_type == "postgres"
    assert [node.unique_id for node in view.models()] == ["model.p.orders"]
    orders = view.get("model.p.orders")
    assert orders is not None and orders.depends_on.nodes == ["source.p.raw.orders"]
    assert view.get("seed.p.countries") is view.nodes["seed.p.countries"]
    assert view.get("model.p.missing") is None
    assert view.sources == {"source.p.raw.orders": source}


@pytest.mark.parametrize("schema", SUPPORTED_MANIFEST_SCHEMAS)
def test_load_manifest_accepts_supported_schemas_without_nodes_or_sources(tmp_path, schema):
    view = load_manifest(_write(tmp_path, {"metadata": {"dbt_schema_version": schema, "dbt_version": "1.8.0"}}))
    assert view.metadata.dbt_version == "1.8.0"
    assert view.nodes == {} and view.sources == {}


def test_load_manifest_rejects_unsupported_schema(tmp_path):
    path = _write(tmp_path, _payload(metadata={"dbt_schema_version": V12.replace("v12", "v99"), "dbt_version": "1"}))
    with pytest.raises(ArtifactError) as exc_info:
        load_manifest(path)
    assert exc_info.value.reason_code is ReasonCode.UNSUPPORTED_MANIFEST_SCHEMA


_VALID_JSON = json.dumps(_payload()).encode("utf-8")


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (_VALID_JSON.replace(b"1.9.0", b"1.9.0\xff"), "not valid UTF-8"),
        (b"\xef\xbb\xbf" + _VALID_JSON, "invalid manifest JSON"),
        (_VALID_JSON[:-1], "invalid manifest JSON"),
        (b'{"metadata": {}, "metadata": {}}', "duplicate JSON key: metadata"),
        (_VALID_JSON[:-1] + b', "n": ' + b"1" * 5000 + b"}", "invalid manifest JSON"),
        # Python <= 3.13 raises RecursionError while decoding; 3.14 decodes it and the structure is refused.
        (b"[" * 100_000 + b"]" * 100_000, None),
    ],
    ids=["non-utf8", "utf8-bom", "truncated", "duplicate-key", "huge-integer", "deep-nesting"],
)
def test_load_manifest_rejects_undecodable_bytes(tmp_path, content, message):
    # A bare UnicodeDecodeError, ValueError or RecursionError escaped here and crashed scan, which only
    # tolerates RefmergeError from a stale target/manifest.json.
    with pytest.raises(ArtifactError, match=message) as exc_info:
        load_manifest(_write(tmp_path, content))
    assert exc_info.value.reason_code is ReasonCode.INTERNAL_ERROR


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ([], "root must be an object"),
        ({"nodes": {}}, "metadata missing"),
        (_payload(metadata=[V12]), "metadata missing"),
        (_payload(metadata={"dbt_schema_version": V12}), "invalid manifest metadata"),
        (_payload(nodes=[]), "nodes must be an object"),
        (_payload(nodes=None), "nodes must be an object"),
        (_payload(nodes={"model.p.m": _node("model.p.m", name=None)}), "invalid node model.p.m"),
        (_payload(nodes={"model.p.m": {"resource_type": "model"}}), "invalid node model.p.m"),
        (_payload(nodes={"model.p.m": "model.p.m"}), "invalid node model.p.m"),
        (_payload(nodes={"model.p.m": None}), "invalid node model.p.m"),
        (_payload(nodes={"model.p.m": _node("model.p.other")}), "does not match its unique_id"),
        (_payload(sources=[]), "sources must be an object"),
        (_payload(sources={"source.p.raw.orders": ["raw", "orders"]}), "invalid source source.p.raw.orders"),
    ],
    ids=[
        "root-not-object",
        "metadata-absent",
        "metadata-not-object",
        "metadata-missing-dbt-version",
        "nodes-list",
        "nodes-null",
        "node-invalid-field",
        "node-without-unique-id",
        "node-string",
        "node-null",
        "node-key-mismatch",
        "sources-list",
        "source-not-object",
    ],
)
def test_load_manifest_rejects_malformed_structure(tmp_path, payload, message):
    with pytest.raises(ArtifactError, match=message) as exc_info:
        load_manifest(_write(tmp_path, payload))
    assert exc_info.value.reason_code is ReasonCode.INTERNAL_ERROR


def test_load_manifest_refuses_non_object_node_that_would_hide_ambiguity(tmp_path):
    # Silently dropping model.q.stg turned an ambiguous ref('stg') into a unique model.p.stg.
    owner = _node("model.p.m", depends_on={"nodes": ["model.p.stg", "model.q.stg"]})
    payload = _payload(nodes={"model.p.m": owner, "model.p.stg": _node("model.p.stg"), "model.q.stg": ["stg"]})
    with pytest.raises(ArtifactError, match="invalid node model.q.stg") as exc_info:
        load_manifest(_write(tmp_path, payload))
    assert exc_info.value.reason_code is ReasonCode.INTERNAL_ERROR


@pytest.mark.parametrize(
    "original_file_path",
    [
        "/etc/m.sql",
        "../m.sql",
        "models/../../m.sql",
        "..\\m.sql",
        "models\\..\\..\\m.sql",
        "\\\\server\\share\\m.sql",
        "C:\\evil\\m.sql",
        "c:/evil/m.sql",
        "C:m.sql",
        "//server/share/m.sql",
    ],
)
def test_load_manifest_rejects_unsafe_original_file_path(tmp_path, original_file_path):
    # S10: drive-letter and UNC paths escape the project when joined on Windows.
    payload = _payload(nodes={"model.p.m": _node("model.p.m", original_file_path=original_file_path)})
    with pytest.raises(ArtifactError, match="unsafe original_file_path: model.p.m") as exc_info:
        load_manifest(_write(tmp_path, payload))
    assert exc_info.value.reason_code is ReasonCode.INTERNAL_ERROR


@pytest.mark.parametrize("original_file_path", ["models/staging/m.sql", "models\\staging\\m.sql", "models/..m/m.sql"])
def test_load_manifest_accepts_project_relative_original_file_path(tmp_path, original_file_path):
    payload = _payload(nodes={"model.p.m": _node("model.p.m", original_file_path=original_file_path)})
    view = load_manifest(_write(tmp_path, payload))
    assert view.nodes["model.p.m"].original_file_path == original_file_path


def test_load_manifest_rejects_oversized(tmp_path):
    path = tmp_path / "manifest.json"
    with path.open("wb") as fh:
        fh.truncate(MAX_ARTIFACT_BYTES + 1)  # sparse: never writes 256 MB
    with pytest.raises(ArtifactError, match=f"manifest too large: {MAX_ARTIFACT_BYTES + 1} bytes") as exc_info:
        load_manifest(path)
    assert exc_info.value.reason_code is ReasonCode.INTERNAL_ERROR


@pytest.mark.parametrize("relative", ["missing.json", "."], ids=["missing", "directory"])
def test_load_manifest_rejects_unreadable_path(tmp_path, relative):
    with pytest.raises(ArtifactError, match="cannot read manifest") as exc_info:
        load_manifest(tmp_path / relative)
    assert exc_info.value.reason_code is ReasonCode.INTERNAL_ERROR


# -- resolve_literal_ref -----------------------------------------------------------------------------


def _model(
    uid: str,
    *,
    depends_on: tuple[str, ...] = (),
    refs: tuple[dict[str, object], ...] = (),
    sources: tuple[tuple[str, str], ...] = (),
) -> ManifestNodeModel:
    resource_type, package, name = uid.split(".")[:3]
    return ManifestNodeModel(
        unique_id=uid,
        resource_type=resource_type,
        package_name=package,
        name=name,
        original_file_path=f"models/{name}.sql",
        depends_on=DependsOnModel(nodes=list(depends_on)),
        refs=[RefArgsModel.model_validate(ref) for ref in refs],
        sources=[list(pair) for pair in sources],
    )


def _view(*nodes: ManifestNodeModel, sources: dict[str, object] | None = None) -> ManifestView:
    return ManifestView(
        metadata=ManifestMetadataModel(dbt_schema_version=V12, dbt_version="1.9.0"),
        nodes={node.unique_id: node for node in nodes},
        sources=sources or {},
        path=Path("manifest.json"),
    )


def _refuses(view: ManifestView, owner: ManifestNodeModel, *args: str | None) -> ReasonCode:
    with pytest.raises(ArtifactError) as exc_info:
        resolve_literal_ref(view, owner, *args)
    return exc_info.value.reason_code


@pytest.mark.parametrize("upstream", ["model.p.stg", "seed.p.stg", "snapshot.p.stg"])
def test_resolve_ref_accepts_models_seeds_and_snapshots(upstream):
    owner = _model("model.p.m", depends_on=(upstream,))
    assert resolve_literal_ref(_view(owner, _model(upstream)), owner, "ref", None, "stg") == upstream


def test_resolve_ref_skips_dependencies_that_cannot_be_the_ref():
    owner = _model(
        "model.p.m",
        depends_on=(
            "source.p.raw.stg",  # not a node
            "analysis.p.stg",  # not refable
            "model.p.other",  # different name
            "model.q.stg",  # different package
            "model.p.stg",
            "model.p.stg",  # listed twice
        ),
    )
    nodes = (_model("analysis.p.stg"), _model("model.p.other"), _model("model.q.stg"), _model("model.p.stg"))
    assert resolve_literal_ref(_view(owner, *nodes), owner, "ref", "p", "stg") == "model.p.stg"


def test_resolve_ref_ambiguous_across_packages():
    owner = _model("model.p.m", depends_on=("model.p.stg", "model.q.stg"))
    view = _view(owner, _model("model.p.stg"), _model("model.q.stg"))
    assert _refuses(view, owner, "ref", None, "stg") is ReasonCode.SOURCE_MAPPING_AMBIGUOUS


def test_resolve_ref_without_candidates_refuses():
    owner = _model("model.p.m", depends_on=("model.p.other",))
    view = _view(owner, _model("model.p.other"))
    assert _refuses(view, owner, "ref", None, "stg") is ReasonCode.SOURCE_MAPPING_AMBIGUOUS


def test_resolve_ref_package_arg_disambiguates():
    owner = _model("model.p.m", depends_on=("model.p.stg", "model.q.stg"))
    view = _view(owner, _model("model.p.stg"), _model("model.q.stg"))
    assert resolve_literal_ref(view, owner, "ref", "q", "stg") == "model.q.stg"


def test_resolve_ref_refs_metadata_disambiguates():
    owner = _model(
        "model.p.m",
        depends_on=("model.p.stg", "model.q.stg", "model.p.dim"),
        refs=({"name": "dim"}, {"package": "q", "name": "stg"}),
    )
    view = _view(owner, _model("model.p.stg"), _model("model.q.stg"), _model("model.p.dim"))
    assert resolve_literal_ref(view, owner, "ref", None, "stg") == "model.q.stg"


def test_resolve_ref_refs_metadata_matching_every_candidate_stays_ambiguous():
    owner = _model("model.p.m", depends_on=("model.p.stg", "model.q.stg"), refs=({"name": "stg"},))
    view = _view(owner, _model("model.p.stg"), _model("model.q.stg"))
    assert _refuses(view, owner, "ref", None, "stg") is ReasonCode.SOURCE_MAPPING_AMBIGUOUS


@pytest.mark.parametrize(
    "refs",
    [({"name": "other"},), ({"package": "q", "name": "stg"},)],
    ids=["refs-name-other", "refs-package-other"],
)
def test_resolve_ref_refs_metadata_contradicting_every_candidate_refuses(refs):
    # The manifest says the model never calls this ref: the literal call and the manifest disagree.
    owner = _model("model.p.m", depends_on=("model.p.stg",), refs=refs)
    view = _view(owner, _model("model.p.stg"))
    assert _refuses(view, owner, "ref", None, "stg") is ReasonCode.SOURCE_MAPPING_AMBIGUOUS


_ORDERS = {"source_name": "raw", "name": "orders"}


@pytest.mark.parametrize("owner_sources", [(), (("raw", "customers"), ("raw", "orders"))], ids=["no-meta", "meta"])
def test_resolve_source_matches_source_and_table_name(owner_sources):
    owner = _model("model.p.m", depends_on=("source.p.raw.orders",), sources=owner_sources)
    sources = {
        "source.p.raw.customers": {"source_name": "raw", "name": "customers"},
        "source.p.other.orders": {"source_name": "other", "name": "orders"},
        "source.p.raw.orders": _ORDERS,
    }
    view = _view(owner, sources=sources)
    assert resolve_literal_ref(view, owner, "source", None, "orders", "raw") == "source.p.raw.orders"


def test_resolve_source_ambiguous_same_name_two_packages():
    owner = _model("model.p.m", depends_on=("source.p.raw.orders",), sources=(("raw", "orders"),))
    view = _view(owner, sources={"source.p.raw.orders": _ORDERS, "source.q.raw.orders": _ORDERS})
    assert _refuses(view, owner, "source", None, "orders", "raw") is ReasonCode.SOURCE_MAPPING_AMBIGUOUS


def test_resolve_source_without_source_name_refuses():
    owner = _model("model.p.m", depends_on=("source.p.raw.orders",))
    view = _view(owner, sources={"source.p.raw.orders": _ORDERS})
    assert _refuses(view, owner, "source", None, "orders", None) is ReasonCode.SOURCE_MAPPING_AMBIGUOUS


def test_resolve_source_owner_sources_without_match_refuses():
    # The manifest says the model never calls source('raw', 'orders'): refuse rather than trust the literal.
    owner = _model("model.p.m", depends_on=("source.p.raw.orders",), sources=(("raw", "customers"),))
    view = _view(owner, sources={"source.p.raw.orders": _ORDERS})
    assert _refuses(view, owner, "source", None, "orders", "raw") is ReasonCode.SOURCE_MAPPING_AMBIGUOUS


@pytest.mark.parametrize("depends_on_broken", [True, False], ids=["in-depends-on", "elsewhere"])
def test_resolve_source_non_dict_entry_fails_closed(depends_on_broken):
    # An entry that is not an object has no name to compare, so it may be another candidate.
    depends_on = ("source.p.raw.orders", "source.q.raw.orders") if depends_on_broken else ("source.p.raw.orders",)
    owner = _model("model.p.m", depends_on=depends_on)
    view = _view(owner, sources={"source.p.raw.orders": _ORDERS, "source.q.raw.orders": ["raw", "orders"]})
    assert _refuses(view, owner, "source", None, "orders", "raw") is ReasonCode.INTERNAL_ERROR
