"""Downstream CTE references and scan-time duplicate detection in the source frontend."""

import pytest

from dbt_refmerge.source import parse_source_model

_IMPORTS = "with a as (select id from {{ ref('m') }}), b as (select id from {{ ref('m') }}), "


def _refs(tail: str) -> list[tuple[str, str, bool, bool]]:
    """(identity, source bytes, has_alias, star_expansion) for each downstream reference."""
    raw = (_IMPORTS + tail).encode()
    model = parse_source_model(raw)
    return [
        (
            ref.cte_identity.value,
            raw[ref.span.start_byte : ref.span.end_byte].decode(),
            ref.has_alias,
            ref.star_expansion,
        )
        for ref in model.downstream_refs
    ]


@pytest.mark.parametrize(
    "final",
    [
        "final as (select x.* from b as `x`) select 1",
        "final as (select `x`.* from b as x) select 1",
        "final as (select x.* from b as) select 1",
    ],
    ids=["backtick-alias", "backtick-qualifier", "missing-alias"],
)
def test_qualified_star_through_unreadable_alias_counts_as_star_expansion(final):
    # An alias the frontend cannot resolve may be the qualifier of any `q.*` in the scope.
    assert _refs(final) == [("b", "b", True, True)]
