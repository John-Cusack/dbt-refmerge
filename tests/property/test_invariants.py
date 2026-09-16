"""Property tests: rewrite invariants."""

from hypothesis import given, settings
from hypothesis import strategies as st

from dbt_refmerge.domain import ReasonCode, SourceSpan, TextEdit
from dbt_refmerge.rewrite import apply_edits, validate_edits

ident = st.from_regex(r"[a-z][a-z0-9_]{0,7}", fullmatch=True)


@given(st.lists(st.integers(min_value=1, max_value=5), min_size=1, max_size=4))
@settings(max_examples=50)
def test_non_overlapping_edits_validate(sizes):
    src = b"x" * sum(sizes)
    edits = []
    off = 0
    for s in sizes:
        edits.append(TextEdit(span=SourceSpan(off, off + s), replacement=b"y" * s, reason_code=ReasonCode.OK))
        off += s
    validate_edits(tuple(edits), len(src))
    out = apply_edits(src, tuple(edits))
    assert len(out) == len(src)


@given(ident, ident)
@settings(max_examples=50)
def test_identifier_lowering_stable(a, b):
    from dbt_refmerge.source import make_identifier

    assert make_identifier(a, False).identity.value == a.lower()
    assert make_identifier(f'"{b}"', True).identity.value == b
