# Next Odd-Scenario Regression Tests

This document is the canonical, deduplicated regression-test strategy for `dbt-refmerge`. It reconciles the original proposal with `test-strategy.opus.md`; overlapping cases are listed once, and unsafe Opus expectations were corrected to follow the project's fail-closed policy. All 45 numbered scenarios are implemented, and every heading uses the exact pytest function name. The first group records cases that previously produced an unsafe plan, an incorrect no-op, or `INTERNAL_ERROR`. The second group pins behavior that is correct or conservatively fail-closed.

The implemented fast regression lane is `tests/unit/test_odd_scenarios.py`. It exercises these cases entirely in memory so the complete odd-scenario file remains sub-second. `qualify_group` consumes `downstream_refs`, `build_plan` reparses candidates with `model.fold_unquoted`, and the canonical comment/Jinja guard scans from `select_list_span.end_byte` through the canonical tail.

## Common fixture

Unless a test says otherwise, use this source shape:

```sql
with a as (
    select
        id,
        customer_id
    from {{ ref('stg') }}
),
b as (
    select
        id,
        amount
    from {{ ref('stg') }}
),
...
```

The compiled SQL must mirror the source exactly, replacing each `{{ ref('stg') }}` with `db.sch.stg`. The manifest maps both calls to `model.p.stg`.

Tests should remain plain pytest functions with no classes or mocks. Assert observable candidate SQL, status and exact reason codes, or the documented public error. Byte-sensitive tests must compare complete `bytes`, not normalized text.

## Implemented regressions that exposed bugs

These are ordered from highest silent-corruption risk downward.

### 1. `test_added_projection_cannot_capture_unqualified_downstream_column`

Source tail:

```sql
final as (
    select customer_id
    from b
    join (select id, customer_id from dim_customers) d using (id)
)
select customer_id from final
```

The compiled SQL mirrors this tail.

Assertion: `NOT_ELIGIBLE` with exactly:

```python
(ReasonCode.REFERENCE_BINDING_AMBIGUOUS,)
```

Why it matters: after projection union, rewritten `a AS b` unexpectedly gains `customer_id`, making the previously valid unqualified reference ambiguous. This pins the downstream column-binding check performed from `qualify_group`'s `downstream_refs`.

### 2. `test_downstream_qualified_star_refuses_projection_union`

Source tail:

```sql
select b.*
from b
```

The compiled SQL mirrors it.

Assertion: `NOT_ELIGIBLE` with exactly:

```python
(ReasonCode.UNSUPPORTED_IMPORT_SHAPE,)
```

Why it matters: `b.*` originally exposes `(id, amount)` but exposes `(id, customer_id, amount)` after rewriting to `a AS b`. This pins propagation of downstream star expansion into the fail-closed qualification gate.

### 3. `test_comma_join_donor_binding_refuses`

Source tail:

```sql
final as (
    select a.id
    from a, b
    where a.id = b.id
)
select * from final
```

The compiled SQL mirrors it.

Assertion: `NOT_ELIGIBLE` with exactly:

```python
(ReasonCode.REFERENCE_BINDING_AMBIGUOUS,)
```

Why it matters: the former reference scanner missed comma-bound `b`, deleted its CTE, and left a dangling reference. This pins comma-bound relations as ambiguous downstream references.

### 4. `test_cross_join_keyword_is_not_an_implicit_alias`

Source tail:

```sql
final as (
    select b.id
    from b cross join dim
)
select * from final
```

The compiled SQL mirrors it.

Assertion: successful output contains:

```sql
from a as b cross join dim
```

It must contain no `b as (` import CTE.

Why it matters: treating `CROSS` as an implicit alias emits `from a cross join dim` while leaving `b.id` unchanged. This pins `CROSS` as an alias-stopper keyword.

### 5. `test_natural_join_refuses_projection_union`

Source tail:

```sql
final as (
    select a.id
    from a natural join b
)
select * from final
```

The compiled SQL mirrors it.

Assertion: `NOT_ELIGIBLE` with exactly:

```python
(ReasonCode.REFERENCE_BINDING_AMBIGUOUS,)
```

Why it matters: unioning projections adds `customer_id` and `amount` to both relation instances, changing the implicit `NATURAL JOIN` key set.

### 6. `test_jinja_block_straddling_donor_deletion_refuses`

Source:

```sql
with a as (...),
{% if var('include_b') %}
b as (...)
{% endif %},
final as (
    select b.id from b
)
select * from final
```

The compiled SQL mirrors the rendered `include_b=true` form, with the Jinja tags absent.

Assertion: `build_plan` raises `RewriteError` with exactly:

```python
assert exc_info.value.reason_code is ReasonCode.COMMENT_RELOCATION_UNSUPPORTED
```

Why it matters: donor deletion starts at `b` but extends through the separator after `{% endif %}`, which would leave an unmatched opening block. The deletion guard now rejects any overlapping non-`ref` Jinja span.

### 7. `test_donor_comments_outside_select_span_refuse`

Use two parametrized cases in one plain test function.

Terminal projection comment:

```sql
amount -- donor explanation
from {{ ref('stg') }}
```

Comment between donor close and separator:

```sql
b as (...) /* donor ownership note */,
```

The compiled SQL omits the comments but otherwise mirrors the source.

Assertion for each case: `build_plan` raises `RewriteError` with exactly:

```python
assert exc_info.value.reason_code is ReasonCode.COMMENT_RELOCATION_UNSUPPORTED
```

Why it matters: deleting either comment loses user-authored bytes. The rewrite guard scans the complete donor deletion span, including tail and separator ownership.

### 8. `test_canonical_terminal_comment_refuses_projection_append`

Canonical ending:

```sql
customer_id -- describes customer_id
from {{ ref('stg') }}
```

The donor contributes `amount`; compiled SQL mirrors the source.

Assertion: `build_plan` raises `RewriteError` with exactly:

```python
assert exc_info.value.reason_code is ReasonCode.COMMENT_RELOCATION_UNSUPPORTED
```

Why it matters: insertion immediately after `customer_id` would turn the comment into an annotation on appended `amount`. The guard scans from `select_list_span.end_byte` through the canonical tail before inserting.

### 9. `test_none_fold_predicate_case_remains_semantic`

Use the ClickHouse dialect with `fold_unquoted="none"`:

```sql
with a as (
    select id
    from {{ ref('stg') }}
    where Status = 1
),
b as (
    select id
    from {{ ref('stg') }}
    where status = 1
)
select a.id from a join b on a.id = b.id
```

The compiled SQL preserves both predicate spellings.

Assertion: `NOT_ELIGIBLE` with exactly:

```python
(ReasonCode.DIFFERENT_PREDICATE,)
```

Why it matters: lowercasing every unquoted identifier regardless of dialect makes these predicates hash identically. This pins dialect-aware predicate fingerprints.

### 10. `test_quoted_cte_case_maps_without_folding`

PostgreSQL source:

```sql
with "A" as (
    select
        id,
        customer_id
    from {{ ref('stg') }}
),
"B" as (
    select
        id,
        amount
    from {{ ref('stg') }}
),
final as (
    select "B".amount from "B"
)
select * from final
```

The compiled SQL preserves `"A"` and `"B"`.

Assertion: successful merge retaining `"A"` and rewriting the relation as:

```sql
"A" as "B"
```

The flow must not report `SOURCE_MAPPING_AMBIGUOUS` or later `COMPILE_DRIFT`.

Why it matters: reading compiled CTE quote state from `TableAlias` instead of its child `Identifier` indexes `"A"` as folded `a`. This pins quote preservation in both source matching and expected transforms.

### 11. `test_none_fold_reparse_preserves_case_distinct_ctes`

Use ClickHouse and include the duplicate-import group plus unrelated, valid case-distinct CTEs:

```sql
Foo as (
    select 1 as x
),
foo as (
    select 2 as x
),
final as (
    select b.id from b
)
```

The compiled SQL mirrors the same spellings.

Assertion: successful exact merge preserving both `Foo` and `foo`.

Why it matters: reparsing with default lower-folding causes a false duplicate-name error wrapped as `INTERNAL_ERROR`. This pins candidate reparsing with `model.fold_unquoted`.

### 12. `test_bigquery_trailing_comma_style_merges`

BigQuery source:

```sql
with a as (
    select
        id,
        customer_id,
    from {{ ref('stg') }}
),
b as (
    select
        id,
        amount,
    from {{ ref('stg') }}
),
final as (
    select a.id from a join b using (id)
)
select * from final
```

The compiled SQL retains valid BigQuery trailing commas.

Assertion: exact output with canonical projection list:

```sql
id,
customer_id,
amount,
```

The donor is removed and formatting is otherwise byte-identical.

Why it matters: the planner supports trailing commas, so the frontend must skip the empty final projection rather than yielding a false `NO_DUPLICATE_IMPORT`.

### 13. `test_unquoted_unicode_projection_merges`

PostgreSQL source uses `café_id` and `montant` as unquoted projection identifiers. The compiled SQL preserves those names.

Assertion: successful merge with exact UTF-8 output bytes.

Why it matters: tokenization accepts Unicode identifiers, so later projection validation must not silently remove those imports from grouping with an ASCII-only check.

### 14. `test_decode_source_pure_crlf_is_not_mixed`

Frontend-only scenario: a complete valid source represented entirely with `b"\r\n"`. Compiled SQL is not applicable.

Assertions:

```python
assert decoded.newline_style == "crlf"
assert candidate == exact_expected_crlf_bytes
```

Why it matters: newline classification must remove CRLF pairs before looking for lone CR or LF bytes. The existing CRLF rewrite golden did not pin this metadata.

## Implemented pins for correct or conservatively fail-closed behavior

These are ordered by semantic risk, then byte-exactness risk.

### 15. `test_bare_and_aliased_donor_self_join_redirects_every_binding`

Source tail:

```sql
final as (
    select b.id, rhs.amount
    from b
    join b as rhs using (id)
)
select * from final
```

The compiled SQL mirrors it.

Assertion: exact output contains:

```sql
from a as b
join a as rhs using (id)
```

Why it matters: pins bare-name alias synthesis and explicit-alias preservation across multiple bindings of the donor.

### 16. `test_four_members_one_divergent_filter_refuses_whole_group`

Use four imports from `stg`. Imports `a`, `b`, and `c` use:

```sql
where active = true
```

Import `d` uses:

```sql
where active = false
```

Compiled SQL mirrors all four imports.

Assertion: exactly:

```python
assert qualified.status == FindingStatus.NOT_ELIGIBLE
assert qualified.reason_codes == (ReasonCode.DIFFERENT_PREDICATE,)
```

No partial three-member plan should be built.

Why it matters: prevents unsafe subset merging and order-dependent qualification.

### 17. `test_nested_cte_shadow_refuses_end_to_end`

Add this downstream CTE:

```sql
wrapper as (
    with b as (
        select id from dim
    )
    select id from b
)
```

The compiled SQL mirrors it.

Assertion: exactly `NOT_ELIGIBLE` with:

```python
(ReasonCode.REFERENCE_BINDING_AMBIGUOUS,)
```

Why it matters: pins conservative behavior where a donor spelling is shadowed in a nested scope.

### 18. `test_nondeterministic_downstream_refuses_merge`

Source tail:

```sql
select random(), b.id
from b
```

The compiled SQL mirrors it.

Assertion: exactly `NOT_ELIGIBLE` with:

```python
(ReasonCode.NONDETERMINISTIC,)
```

Why it matters: pins propagation of the whole-model volatility gate into group status.

### 19. `test_unreferenced_dead_donor_is_removed_without_redirect`

The final query explicitly selects only:

```sql
select a.id
from a
```

The donor `b` is never referenced. Compiled SQL mirrors the source.

Assertions:

- Exact output deletes `b`.
- `amount` is appended to `a`.
- No synthetic `AS b` reference is created.

Why it matters: pins the intentional dead-import policy and prevents later regressions that incorrectly require every donor to have a downstream reference.

### 20. `test_donors_separated_by_unrelated_cte_preserve_middle_block`

CTE order:

```sql
a as (/* import */),
mid as (
    select a.id from a
),
b as (/* duplicate import */),
final as (
    select b.amount from b
)
```

The compiled SQL mirrors the same order.

Assertions:

- `mid` is retained byte-for-byte.
- Only `b` is deleted.
- `amount` is appended to `a`.
- The final reference is redirected with donor alias preservation.

Why it matters: exercises comma ownership and nonadjacent group membership.

### 21. `test_single_line_canonical_needs_insertion_refuses`

Canonical:

```sql
a as (
    select
        id
    from {{ ref('stg') }}
)
```

Donor:

```sql
b as (
    select
        id,
        amount
    from {{ ref('stg') }}
)
```

Compiled SQL mirrors both imports.

Assertion: `build_plan` raises `RewriteError` with exactly:

```python
ReasonCode.UNSUPPORTED_IMPORT_SHAPE
```

Why it matters: a one-token `select_list_span` carries no newline or indentation evidence. The current conservative refusal should remain pinned (`src/dbt_refmerge/rewrite.py:92`).

### 22. `test_comment_inside_donor_select_refuses_cleanly`

Donor projection:

```sql
amount /* donor meaning */ as amount
```

The compiled projection is:

```sql
amount as amount
```

Assertion: `RewriteError` with exactly:

```python
ReasonCode.COMMENT_RELOCATION_UNSUPPORTED
```

Why it matters: pins the in-span comment guard independently of the boundary-comment cases.

### 23. `test_bom_preserved_through_actual_rewrite`

Source bytes:

```python
raw = b"\xef\xbb\xbf" + BASE_BYTES
```

The compiled SQL is the normal semantic mirror without a BOM.

Assertion:

```python
assert candidate == b"\xef\xbb\xbf" + EXPECTED_BASE_BYTES
```

Why it matters: pins the three-byte offset in every edit span, not merely successful parsing.

### 24. `test_mixed_crlf_lf_file_preserves_local_bytes`

Construct raw bytes so that:

- The canonical CTE uses CRLF.
- The donor and final CTE use LF.
- The canonical projection indentation is fixed and explicit.

The compiled SQL is newline-normalized but structurally identical.

Assertion: compare complete expected bytes. The appended projection uses the canonical CRLF and indentation, while every untouched LF remains LF.

Why it matters: prevents global newline normalization and wrong local insertion style.

### 25. `test_tab_indented_select_list_insertion`

Both import lists use:

```text
\tselect
\t\tid,
\t\tcustomer_id
```

The compiled SQL mirrors the structure.

Assertion: exact output bytes contain appended `\t\tamount`, not spaces.

Why it matters: pins `_detect_newline_indent` for tabs.

### 26. `test_no_trailing_newline_remains_absent`

The valid source ends immediately after:

```sql
select id from final
```

There is no final `\n` or `\r`. Compiled SQL mirrors the source.

Assertions:

```python
assert candidate == expected
assert not candidate.endswith(b"\n")
assert not candidate.endswith(b"\r")
```

Why it matters: prevents deletion cleanup from manufacturing a final newline.

### 27. `test_unicode_prefix_and_quoted_identifier_spans_are_byte_exact`

Source begins with:

```sql
-- π
```

It uses CTE name `café`, quoted projection `"café_id"`, and a donor that adds `montant`. The compiled SQL mirrors those identifiers.

Assertion: exact UTF-8 output bytes, including the prefix comment and redirected Unicode CTE name.

Why it matters: pins `char_to_byte` offsets when multibyte characters precede every edited span.

### 28. `test_cr_only_file_insertion_refuses_cleanly`

The entire source uses `\r` as its line separator. The donor adds a missing projection. Compiled SQL is the normalized semantic mirror.

Assertion: `RewriteError` with exactly:

```python
ReasonCode.UNSUPPORTED_IMPORT_SHAPE
```

It must never become `INTERNAL_ERROR`.

Why it matters: `_detect_newline_indent` recognizes only LF and CRLF today. This locks in a clean refusal for CR-only input.

### 29. `test_quoted_and_unquoted_same_identity_collide`

Use parametrized dialect cases:

- PostgreSQL/lower: `amount AS x` versus `region AS "x"`.
- Snowflake/upper: `amount AS X` versus `region AS "X"`.
- ClickHouse/none: `amount AS X` versus `region AS "X"`.

Compiled SQL preserves quote state.

Assertion for every case: exactly `NOT_ELIGIBLE` with:

```python
(ReasonCode.PROJECTION_COLLISION,)
```

Why it matters: quoted identifiers never fold, but may still equal the dialect-resolved unquoted spelling.

### 30. `test_case_different_outputs_follow_dialect_fold`

Use:

```sql
amount as Key
```

versus:

```sql
region as key
```

Expected results:

- PostgreSQL/lower: `NOT_ELIGIBLE / PROJECTION_COLLISION`.
- Snowflake/upper: `NOT_ELIGIBLE / PROJECTION_COLLISION`.
- ClickHouse/none: successful union retaining both `Key` and `key`.

The compiled SQL uses the corresponding dialect spelling.

Why it matters: pins all three `FoldRule` behaviors at the semantic and rewrite boundary.

### 31. `test_unsupported_cte_delimiters_refuse_cleanly`

Use parametrized source cases.

BigQuery:

```sql
with `a` as (...),
`b` as (...)
select * from `a`
```

T-SQL:

```sql
with [a] as (...),
[b] as (...)
select * from [a]
```

The compiled fixtures use the same delimiters with physical relations substituted.

For the current conservative policy, assert `SourceParseError` with exactly:

```python
ReasonCode.UNSUPPORTED_IMPORT_SHAPE
```

It must never raise `INTERNAL_ERROR` or accidentally group identifiers under the wrong folding rule.

Why it matters: pins safe handling until source-side support for backtick and bracket delimiters is implemented.

## Test-harness changes

Minimally extend `tests/unit/test_rewrite.py::_run_case` to accept:

```python
def _run_case(
    raw: str | bytes,
    compiled: str,
    view_owner=None,
    *,
    dialect: str = "postgres",
    fold_unquoted: FoldRule | None = None,
): ...
```

When `fold_unquoted` is omitted, derive it from the dialect's adapter specification. This avoids creating tests whose source and compiled sides accidentally use different identity rules.

Additional test rules:

- Byte-sensitive cases belong in `tests/golden/` or must compare the complete `bytes` value in a unit test.
- Do not use `_norm` for BOM, newline, tab, Unicode, comment, or no-final-newline cases.
- Refusal tests should assert exact `reason_codes` tuples. Use membership assertions only when the deliberate contract includes multiple simultaneous reasons.
- A current `INTERNAL_ERROR` is never the expected result for these cases.
- Do not assert private span coordinates or internal dictionaries; assert candidate SQL, status, reason code, or raised domain error.

## Additional implemented cases

The following scenarios extend the ranked list above and are also implemented:

### 32. `test_single_line_canonical_redundant_donor_merges`

A single-line canonical is allowed when the donor contributes no missing projection. Assert donor deletion and no formatting change to the canonical projection list. This contrasts directly with the clean refusal in test 21.

### 33. `test_last_cte_donor_removes_preceding_separator`

Place the donor last in the `WITH` list, with the main query immediately after it. Assert that the preceding comma is removed, the candidate reparses, and all donor references are redirected.

### 34. `test_semantically_plausible_but_ast_different_predicates_refuse`

Parametrize typed-literal differences (`100` versus `100.0`) and reordered conjunctions. Assert exactly `DIFFERENT_PREDICATE`; do not infer semantic equivalence beyond the literal-sensitive AST fingerprint.

### 35. `test_compound_refusal_reason_codes_have_stable_order`

Combine a predicate mismatch with a projection collision. Assert the exact ordered tuple `(DIFFERENT_PREDICATE, PROJECTION_COLLISION)`.

### 36. `test_duplicate_output_within_single_cte_refuses`

Alias two different upstream columns to the same output inside one import CTE. Assert exactly `PROJECTION_COLLISION`, covering the intra-CTE collision branch separately from cross-CTE collisions.

### 37. `test_mixed_alias_styles_redirect_independently`

Use three different donors in one group: `b` bare, `c AS cc`, and `d dd`. Assert each replacement independently preserves its binding spelling. The same-donor self-join case is covered separately by test 15.

### 38. `test_two_independent_groups_rewrite_in_one_fast_plan`

Put two duplicate groups for two upstream models in one source file. Assert both donors are removed and both downstream bindings are redirected in one plan. Do not assert the internal single-valued `canonical_cte` metadata field.

### 39. `test_backtick_projection_duplicates_report_unsupported_shape`

Two source CTEs with backtick-delimited projections over the same literal `ref()` must produce an observable `UNSUPPORTED_IMPORT_SHAPE` finding. They must not silently disappear into `NO_DUPLICATE_IMPORT`.

### 40. `test_jinja_comma_overlapping_last_donor_deletion_refuses`

Put a non-`ref` Jinja expression containing a comma between the preceding CTE separator and a last-position donor. Assert `build_plan` raises `COMMENT_RELOCATION_UNSUPPORTED`. This pins overlap rather than full containment: the raw comma search can otherwise choose a byte inside the Jinja span and produce malformed Jinja before the candidate reparse reports a misleading `INTERNAL_ERROR`.

### 41. `test_quoted_and_unquoted_cte_names_redirect_with_exact_spelling`

Use a lowercase unquoted canonical CTE and a case-distinct quoted donor. Assert the donor relation is redirected with its quoted binding spelling preserved exactly.

### 42. `test_quoted_case_variants_never_fold`

Use two differently cased quoted output identifiers under an upper-folding dialect. Assert both remain in the projection union because quoted identifiers never fold.

### 43. `test_unquoted_case_variants_collide_under_folding_dialects`

Parametrize lower- and upper-folding dialects with differently cased unquoted aliases backed by different upstream columns. Assert exactly `PROJECTION_COLLISION`.

### 44. `test_sql_comment_comma_overlapping_last_donor_deletion_refuses`

Put a SQL block comment containing a comma between the preceding CTE separator and a last-position donor. Assert `build_plan` raises `COMMENT_RELOCATION_UNSUPPORTED`. This pins overlap using both comment-token boundaries: otherwise the raw comma search can start deletion inside the comment while a start-only guard misses it.

### 45. `test_consecutive_donors_with_last_donor_do_not_overlap_edits`

Use a three-member group where the two donors are consecutive and the second donor is the final CTE. Assert both donors are removed, projections and downstream bindings are merged, the candidate reparses, and no edits overlap. The preceding donor owns its separator; the last donor begins deletion at its own CTE span.

## Opus proposals deliberately rejected or corrected

- The comma-join case does not pin the unsafe dangling-reference output; it now asserts a fail-closed `REFERENCE_BINDING_AMBIGUOUS` result.
- Canonical comment movement is not accepted as a successful rewrite; it refuses with `COMMENT_RELOCATION_UNSUPPORTED`.
- Multi-group tests assert observable SQL, not incomplete internal plan metadata.
- Backtick-projection duplicates do not pin the old false-negative behavior; scan reports `UNSUPPORTED_IMPORT_SHAPE`.
- Bare donor references must become `canonical AS donor`; expecting only the canonical name would break qualified downstream references.

## Validation gates

The completed tranche must pass:

```text
pytest
ruff check
mypy --strict
```
