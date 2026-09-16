"""Jinja masking + offsets."""

from dbt_refmerge.source import decode_source, mask_jinja, parse_source_model


def test_literal_ref_masked_with_sentinel():
    src = b"with a as (select x from {{ ref('m') }}) select * from a"
    m = mask_jinja(decode_source(src))
    assert len(m.ref_calls) == 1
    call = next(iter(m.ref_calls.values()))
    assert (call.kind, call.name) == ("ref", "m")


def test_source_call_two_args():
    src = b"with a as (select x from {{ source('s', 't') }}) select * from a"
    m = mask_jinja(decode_source(src))
    call = next(iter(m.ref_calls.values()))
    assert (call.kind, call.source_name, call.name) == ("source", "s", "t")


def test_dynamic_call_not_sentinel():
    src = b"with a as (select x from {{ ref('m' + var) }}) select * from a"
    m = mask_jinja(decode_source(src))
    assert m.ref_calls == {}


def test_raw_block_masked():
    src = b"select {% raw %}{{ not_jinja }}{% endraw %}"
    m = mask_jinja(decode_source(src))
    assert any(s.kind == "raw" for s in m.jinja_spans)


def test_unicode_byte_offsets():
    src = "with héllo as (select x from {{ ref('m') }}) select * from héllo".encode()
    model = parse_source_model(src)
    assert len(model.ctes) == 1
    span = model.ctes[0].cte_span
    assert src[span.start_byte : span.end_byte].startswith("héllo".encode()[:2])


def test_crlf_and_bom():
    src = b"\xef\xbb\xbfwith a as (\r\nselect x from {{ ref('m') }}\r\n) select * from a"
    model = parse_source_model(src)
    assert len(model.ctes) == 1


def test_simple_call_agrees_with_ast():
    from dbt_refmerge.source import _parse_literal_call, _parse_simple_call

    simple = [
        "ref('m')",
        "  ref(  'm'  )  ",
        "ref('pkg', 'm')",
        "source('s', 't')",
        "ref(\n'm'\n)",
    ]
    for inner in simple:
        fast = _parse_simple_call(inner)
        full = _parse_literal_call(inner)
        assert fast is not None and fast == full
    ast_only = [
        'ref("m")',
        "ref('m', version=1)",
        "ref('m', v=2)",
        "ref('m' + var)",
        "source('s')",
        "other('m')",
    ]
    for inner in ast_only:
        assert _parse_simple_call(inner) is None
    assert _parse_literal_call('ref("m")') is not None
    assert _parse_literal_call("ref('m', version=1)") is not None
    assert _parse_literal_call("ref('m' + var)") is None


def test_nested_cte_shadow_collected():
    src = (
        b"with a as (select x from {{ ref('m') }}), "
        b"outer_q as (with a as (select y from tbl) select * from a) "
        b"select * from outer_q"
    )
    model = parse_source_model(src)
    assert "a" in model.nested_names
