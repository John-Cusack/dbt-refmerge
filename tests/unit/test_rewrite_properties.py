"""Property-based tests of the static pipeline: source -> match -> qualify -> rewrite -> compiled-delta gate.

Hypothesis builds models with two or three import CTEs over one ``ref()``, with random column lists,
aliases, predicates, an unrelated CTE in between and qualified downstream references. Whatever it builds,
the pipeline must either refuse with reason codes or produce a candidate that reparses, keeps exactly one
import, preserves every imported column, and passes the same delta gate ``check`` runs.
"""

import re
from dataclasses import dataclass
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from dbt_refmerge.adapters import get_spec
from dbt_refmerge.analyze import FindingStatus, group_imports, qualify_group
from dbt_refmerge.artifacts import DependsOnModel, ManifestMetadataModel, ManifestNodeModel, ManifestView
from dbt_refmerge.domain import ReasonCode
from dbt_refmerge.errors import RefmergeError
from dbt_refmerge.orchestrator import RefmergeService
from dbt_refmerge.rewrite import apply_edits, build_plan
from dbt_refmerge.semantics import match_source_ctes, parse_model
from dbt_refmerge.source import parse_source_model

COLUMNS = ("customer_id", "amount", "status", "created_at")
PREDICATES = ("", " where amount > 0", " where status = 'paid'")
ALIASES = ("total", "state", "amount", "customer_id")
_REF = re.compile(r"\{\{ ref\('(\w+)'\) \}\}")
PIPELINE_SETTINGS = settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])


@dataclass(frozen=True)
class Import:
    name: str
    projections: tuple[tuple[str, str | None], ...]  # (upstream column, alias or None)
    predicate: str

    @property
    def outputs(self) -> dict[str, str]:
        return {alias or column: column for column, alias in self.projections}


def _compile(raw: str) -> str:
    return _REF.sub(lambda m: f'"db"."sch"."{m.group(1)}"', raw)


@st.composite
def imports(draw, *, same_predicate: bool = False, aliases: bool = True) -> tuple[Import, ...]:
    count = draw(st.integers(min_value=2, max_value=3))
    predicate = draw(st.sampled_from(PREDICATES))
    # Sharing the predicate half the time, and aliasing a quarter of the columns, keeps merges common.
    shared = same_predicate or draw(st.booleans())
    result = []
    for name in ("a", "b", "c")[:count]:
        columns = draw(st.lists(st.sampled_from(COLUMNS), min_size=1, max_size=3, unique=True))
        projections = [("id", None)]
        for column in columns:
            alias = draw(st.sampled_from((None,) * len(ALIASES) * 3 + ALIASES)) if aliases else None
            projections.append((column, None if alias == column else alias))
        result.append(
            Import(
                name=name,
                projections=tuple(projections),
                predicate=predicate if shared else draw(st.sampled_from(PREDICATES)),
            )
        )
    return tuple(result)


def _model(members: tuple[Import, ...], *, unrelated_after: int) -> str:
    ctes = []
    for member in members:
        select_list = ",\n".join(
            f"        {column}" if alias is None else f"        {column} as {alias}"
            for column, alias in member.projections
        )
        ctes.append(
            f"{member.name} as (\n    select\n{select_list}\n    from {{{{ ref('stg') }}}}{member.predicate}\n)"
        )
    ctes.insert(unrelated_after, "unrelated as (\n    select 1 as one\n)")
    # Every downstream reference is qualified, so a column gained by the merge cannot rebind an unqualified name.
    outputs = [f"{member.name}.{output}" for member in members for output in member.outputs if output != "id"]
    joins = " ".join(f"join {member.name} using (id)" for member in members[1:])
    final = f"final as (\n    select {', '.join(outputs)}\n    from {members[0].name} {joins}\n)"
    return "with " + ",\n".join([*ctes, final]) + "\nselect * from final\n"


def _pipeline(raw: str):
    """Run check's static stages in memory; returns (owner, qualified groups, source model)."""
    owner = ManifestNodeModel(
        unique_id="model.p.m",
        resource_type="model",
        package_name="p",
        name="m",
        original_file_path="models/m.sql",
        raw_code=raw,
        compiled_code=_compile(raw),
        depends_on=DependsOnModel(nodes=["model.p.stg"]),
    )
    upstream = ManifestNodeModel(
        unique_id="model.p.stg",
        resource_type="model",
        package_name="p",
        name="stg",
        original_file_path="models/stg.sql",
    )
    view = ManifestView(
        metadata=ManifestMetadataModel(
            dbt_schema_version="https://schemas.getdbt.com/dbt/manifest/v12.json", dbt_version="1.9.0"
        ),
        nodes={owner.unique_id: owner, upstream.unique_id: upstream},
        sources={},
        path=Path("manifest.json"),
    )
    source = parse_source_model(raw.encode())
    matched = match_source_ctes(
        tuple(c for c in source.ctes if c.ref_call is not None), parse_model(owner.compiled_code or ""), view, owner
    )
    qualified = tuple(
        qualify_group(g, downstream_refs=source.downstream_refs, nested_names=source.nested_names)
        for g in group_imports(list(matched), owner.unique_id)
    )
    return owner, qualified, source


def _merge(raw: str):
    """The candidate source, or the reason codes of a refusal."""
    try:
        owner, qualified, source = _pipeline(raw)
    except RefmergeError as exc:
        return None, (exc.reason_code,)
    assert len(qualified) == 1, "every import reads the same ref, so they form one group"
    (group,) = qualified
    if group.status is not FindingStatus.MERGE_ELIGIBLE:
        return None, group.reason_codes
    try:
        plan = build_plan(raw.encode(), owner.unique_id, Path("models/m.sql"), qualified, source)
    except RefmergeError as exc:
        return None, (exc.reason_code,)
    candidate = apply_edits(raw.encode(), plan.edits).decode()
    candidate_node = owner.model_copy(update={"raw_code": candidate, "compiled_code": _compile(candidate)})
    RefmergeService()._validate_delta(owner, candidate_node, qualified, get_spec("postgres"))
    return candidate, (ReasonCode.OK,)


def _surviving_import(candidate: str):
    model = parse_source_model(candidate.encode())
    (survivor,) = [cte for cte in model.ctes if cte.ref_call is not None and cte.ref_call.name == "stg"]
    return survivor


@PIPELINE_SETTINGS
@given(members=imports(), unrelated_after=st.integers(min_value=0, max_value=3))
def test_every_model_is_refused_with_reasons_or_merged_into_one_import_that_keeps_every_column(
    members, unrelated_after
):
    raw = _model(members, unrelated_after=min(unrelated_after, len(members)))

    candidate, codes = _merge(raw)

    if candidate is None:
        assert codes and ReasonCode.OK not in codes
        return
    survivor = _surviving_import(candidate)
    merged = {p.output_identifier.identity.value: p.upstream_identifier.identity.value for p in survivor.projections}
    expected: dict[str, str] = {}
    for member in members:
        expected.update(member.outputs)
    assert merged == expected
    assert survivor.identifier.source_text == members[0].name
    parse_model(_compile(candidate))  # the candidate still compiles to SQL sqlglot can read


@PIPELINE_SETTINGS
@given(members=imports(same_predicate=True, aliases=False), unrelated_after=st.integers(min_value=0, max_value=3))
def test_plain_imports_with_the_same_predicate_always_merge(members, unrelated_after):
    # Keeps the first property honest: without aliases or differing predicates nothing justifies a refusal.
    raw = _model(members, unrelated_after=min(unrelated_after, len(members)))

    candidate, codes = _merge(raw)

    assert codes == (ReasonCode.OK,), raw
    assert candidate is not None and candidate.count("{{ ref('stg') }}") == 1
