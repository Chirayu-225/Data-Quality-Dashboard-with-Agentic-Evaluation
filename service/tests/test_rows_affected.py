from app.canonical.models import CanonicalTable, ColumnSchema, ColumnType
from app.checks.base import CheckResult, MetricCategory
from app.domain.rows_affected import (
    POLICY_FILL_RATE,
    POLICY_REQUIRED_COLUMNS,
    compute_rows_affected,
)


def _table(n: int = 20, notes_blank_every: int = 2) -> CanonicalTable:
    """`notes` is blank in half the rows (reads as optional); `email` is
    filled everywhere except row 3."""
    rows = []
    for i in range(n):
        rows.append(
            {
                "email": "" if i == 3 else f"u{i}@x.com",
                "notes": "" if i % notes_blank_every == 0 else "hi",
            }
        )
    cols = [ColumnSchema(name="email", type=ColumnType.STRING), ColumnSchema(name="notes", type=ColumnType.STRING)]
    return CanonicalTable(tenant_id="t", source_id="s", table_name="t", columns=cols, rows=rows)


def _null_check(table: CanonicalTable) -> CheckResult:
    flagged, fields = [], {}
    for i, row in enumerate(table.rows):
        blank = [c for c in ("email", "notes") if row.get(c) in (None, "")]
        if blank:
            flagged.append(i)
            fields[i] = blank
    return CheckResult(
        check_name="null_check", metric=MetricCategory.COMPLETENESS,
        flagged_row_indices=flagged, total_rows_evaluated=len(table.rows),
        detail="blank", flagged_fields=fields,
    )


def test_rows_blank_only_in_a_mostly_empty_column_are_not_problems():
    table = _table()
    nc = _null_check(table)
    out = compute_rows_affected([nc], set(), nc.flagged_row_indices, table)
    assert out.policy == POLICY_FILL_RATE
    # row 3 has a blank email (a normally-filled column) -> a problem;
    # the even rows are blank only in `notes` -> not problems.
    assert 3 in out.problem_rows
    assert 4 in out.optional_blank_only_rows
    assert 4 not in out.problem_rows


def test_a_row_with_another_finding_stays_a_problem_even_if_notes_is_blank():
    table = _table()
    nc = _null_check(table)
    dup = CheckResult(
        check_name="duplicate_check", metric=MetricCategory.UNIQUENESS,
        flagged_row_indices=[4], total_rows_evaluated=len(table.rows), detail="dup",
    )
    out = compute_rows_affected([nc, dup], set(), sorted(set(nc.flagged_row_indices) | {4}), table)
    assert 4 in out.problem_rows


def test_a_semantic_flag_makes_a_notes_blank_row_a_problem():
    table = _table()
    nc = _null_check(table)
    out = compute_rows_affected([nc], {6}, nc.flagged_row_indices, table)
    assert 6 in out.problem_rows


def test_configured_required_columns_decide_exactly():
    table = _table()
    nc = _null_check(table)
    out = compute_rows_affected([nc], set(), nc.flagged_row_indices, table, required_columns=["notes"])
    assert out.policy == POLICY_REQUIRED_COLUMNS
    # now `notes` is required, so its blanks ARE problems and email's row 3 is not
    # (row 3 has both blank? i=3 is odd -> notes filled -> email-only blank)
    assert 4 in out.problem_rows
    assert 3 in out.optional_blank_only_rows


def test_null_check_without_field_attribution_is_counted_conservatively():
    table = _table()
    nc = CheckResult(
        check_name="null_check", metric=MetricCategory.COMPLETENESS,
        flagged_row_indices=[0], total_rows_evaluated=len(table.rows), detail="blank",
    )
    out = compute_rows_affected([nc], set(), [0], table)
    assert 0 in out.problem_rows


def test_flagged_row_with_no_known_source_is_never_dropped():
    table = _table()
    out = compute_rows_affected([], set(), [5], table)
    assert 5 in out.problem_rows
