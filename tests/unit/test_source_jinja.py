"""Jinja masking + offsets."""

import pytest

from dbt_refmerge.domain import ReasonCode, RefCall, SourceSpan
from dbt_refmerge.errors import SourceParseError
from dbt_refmerge.source import decode_source, mask_jinja, parse_source_model


def _calls(src: str) -> list[RefCall]:
    return list(mask_jinja(decode_source(src.encode())).ref_calls.values())


@pytest.mark.parametrize(
    "call",
    [
        # dbt ignores unknown ref() kwargs, but a project macro can override ref(); fail closed.
        "{{ ref('m', foo=1) }}",
        # dbt resolves `version or v`; recording either one could name the wrong version.
        "{{ ref('m', version=1, v=2) }}",
        # dbt's source() takes no kwargs at all.
        "{{ source('s', 't', x=1) }}",
        # Star arguments are runtime values: ref('m', *names) can become ref('m', 'n').
        "{{ ref('m', *names) }}",
        "{{ ref('m', **options) }}",
    ],
)
def test_unknown_or_dynamic_call_arguments_are_not_sentinels(call):
    assert _calls(call) == []


@pytest.mark.parametrize(
    ("call", "expected"),
    [
        ("{{- ref('m') -}}", RefCall("ref", None, "m", None, None, SourceSpan(0, 16))),
        ("{{-ref('m')-}}", RefCall("ref", None, "m", None, None, SourceSpan(0, 14))),
        ("{{+ ref('m') }}", RefCall("ref", None, "m", None, None, SourceSpan(0, 15))),
        ("{{- source('s', 't') }}", RefCall("source", None, "t", "s", None, SourceSpan(0, 23))),
        ("{{ ref('m', v=2) -}}", RefCall("ref", None, "m", None, 2, SourceSpan(0, 20))),
    ],
)
def test_whitespace_control_markers_keep_literal_calls(call, expected):
    # The span still covers the markers, so the rewriter treats the whole tag as the ref.
    assert _calls(call) == [expected]


@pytest.mark.parametrize("call", ["{{ ref('m') +}}", "{{ ref('m') --}}", "{{-- ref('m') }}"])
def test_invalid_whitespace_control_is_dynamic(call):
    # Jinja rejects each of these, so none is a literal call.
    assert _calls(call) == []


@pytest.mark.parametrize(
    "src",
    [
        b"select \xff from t",
        b"select {% raw %} x",
        b"select {{ x",
        b"select {% if x",
        b"select {# note",
        b"select {% raw %}{% endraw",
    ],
    ids=["invalid-utf8", "raw", "expression", "statement", "comment", "raw-end"],
)
def test_mask_jinja_refuses_unterminated_or_undecodable(src):
    with pytest.raises(SourceParseError) as exc_info:
        parse_source_model(src)
    assert exc_info.value.reason_code is ReasonCode.INTERNAL_ERROR


@pytest.mark.parametrize(
    "call",
    [
        "{{ ref('m' }}",
        "{{ this }}",
        "{{ foo.ref('m') }}",
        "{{ other('m') }}",
        "{{ ref('m') | upper }}",
        "{{ ref(none) }}",
        "{{ ref(1.5) }}",
        "{{ ref(model_name) }}",
        "{{ ref('m', version=var) }}",
        "{{ ref('m', version=none) }}",
        "{{ ref(1) }}",
        "{{ ref('p', 2) }}",
        "{{ ref('') }}",
        "{{ ref() }}",
        "{{ ref('a', 'b', 'c') }}",
        "{{ source('s') }}",
        "{{ source('s', 't', 'u') }}",
        '{{ source("s", 1) }}',
    ],
)
def test_non_literal_jinja_calls_are_not_sentinels(call):
    assert _calls(call) == []


@pytest.mark.parametrize(
    ("call", "expected"),
    [
        ("{{ ref('m', version=1) }}", RefCall("ref", None, "m", None, 1, SourceSpan(0, 25))),
        ("{{ ref('m', v='2') }}", RefCall("ref", None, "m", None, "2", SourceSpan(0, 21))),
        ("{{ ref('p', 'm') }}", RefCall("ref", "p", "m", None, None, SourceSpan(0, 19))),
        ('{{ ref("p", "m", v=3) }}', RefCall("ref", "p", "m", None, 3, SourceSpan(0, 24))),
        ("{{ ref(\n  'm'\n) }}", RefCall("ref", None, "m", None, None, SourceSpan(0, 18))),
        ('{{ source("s", "t") }}', RefCall("source", None, "t", "s", None, SourceSpan(0, 22))),
    ],
    ids=["version", "v", "package", "package-ast", "multiline", "source-ast"],
)
def test_literal_ref_variants_capture_package_and_version(call, expected):
    assert _calls(call) == [expected]


def test_masking_preserves_newlines_in_every_jinja_kind():
    src = (
        "select {{ ref(\n'm') }}, {{ x\r\n }}, {% if\nx %}{# é\nb #}"
        "{% raw %}\n{{ y }}{% endraw %} from {{ source('s', 't') }}"
    ).encode()
    masked = mask_jinja(decode_source(src))
    assert masked.masked_text == (
        "select "
        + ("__r0__" + " " + "\n" + " " * 7)  # {{ ref(\n'm') }}: sentinel over the first six chars
        + ", "
        + (" " * 4 + "\r\n" + " " * 3)  # {{ x\r\n }}
        + ", "
        + (" " * 5 + "\n" + " " * 4)  # {% if\nx %}
        + (" " * 4 + "\n" + " " * 4)  # {# é\nb #}: one blank per char, not per byte
        + (" " * 9 + "\n" + " " * 19)  # {% raw %}\n{{ y }}{% endraw %}
        + " from "
        + ("__r1__" + " " * 16)  # {{ source('s', 't') }}
    )
    assert [(span.kind, span.span, span.text) for span in masked.jinja_spans] == [
        ("expression", SourceSpan(7, 22), b"{{ ref(\n'm') }}"),
        ("expression", SourceSpan(24, 33), b"{{ x\r\n }}"),
        ("statement", SourceSpan(35, 45), b"{% if\nx %}"),
        ("comment", SourceSpan(45, 55), "{# é\nb #}".encode()),
        ("raw", SourceSpan(55, 84), b"{% raw %}\n{{ y }}{% endraw %}"),
        ("expression", SourceSpan(90, 112), b"{{ source('s', 't') }}"),
    ]
    assert list(masked.ref_calls) == ["__r0__", "__r1__"]
    assert masked.masked_bytes == masked.masked_text.encode()


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
