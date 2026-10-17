from app.agent.generator import GeneratorAgent, ScanResult
from app.agent.semantic_reasoning import SemanticFlag
from app.agent.verifier import VerificationLabel, VerifiedFlag
from app.canonical.models import CanonicalTable, ColumnSchema, ColumnType
from app.checks.base import CheckResult, MetricCategory
from app.scoring.scorer import compute_scorecard


def _table(n_rows: int, columns: list[ColumnSchema] | None = None) -> CanonicalTable:
    """A table with n dummy rows — for tests that only care about row
    count, not schema. Tests that need specific columns (for
    criticality weighting) pass their own `columns`."""
    cols = columns or [ColumnSchema(name="dummy", type=ColumnType.STRING)]
    return CanonicalTable(
        tenant_id="t",
        source_id="s",
        table_name="test_table",
        columns=cols,
        rows=[{c.name: "x" for c in cols} for _ in range(n_rows)],
    )


def _clean_check(metric: MetricCategory, total_rows: int = 10) -> CheckResult:
    """A CheckResult representing "this metric's check(s) ran and found
    nothing" — as distinct from no CheckResult at all ("never ran").
    Tests that want a metric to read as evaluated-and-clean (rather
    than N/A) pass one of these, matching what GeneratorAgent's real
    plan()+execute() would produce for an applicable check with no
    findings."""
    return CheckResult(
        check_name=f"{metric.value}_check",
        metric=metric,
        flagged_row_indices=[],
        total_rows_evaluated=total_rows,
        detail="clean",
    )


def _scan_result(
    check_results=None,
    semantic_flags=None,
    verified_flags=None,
    semantic_iterations=None,
    quarantined_row_indices=None,
    semantic_rows_considered=None,
) -> ScanResult:
    all_flagged = set()
    for r in check_results or []:
        all_flagged.update(r.flagged_row_indices)
    for f in semantic_flags or []:
        # mirror what GeneratorAgent does: exclude rejected from the union
        verified = {v.row_index: v for v in (verified_flags or [])}.get(f.row_index)
        if verified is None or verified.label != VerificationLabel.REJECTED:
            all_flagged.add(f.row_index)

    # A test that supplies semantic_flags is implicitly saying "the
    # semantic layer ran" — default semantic_iterations to 1 in that
    # case so ACCURACY reads as evaluated, same as a real scan (where
    # semantic_flags never appear without semantic_iterations > 0).
    # Explicit semantic_iterations (including 0) always wins.
    if semantic_iterations is None:
        semantic_iterations = 1 if semantic_flags else 0

    quarantined = sorted(quarantined_row_indices or [])

    # semantic_rows_considered is the real denominator ACCURACY uses
    # (see scorer.py's Bug 3 fix). Tests in this file all build tables
    # via _table(10), so — absent an explicit value — assume the
    # semantic layer reliably reasoned over every non-quarantined row,
    # matching what a real, uninterrupted scan of that table would
    # report. A test exercising an interrupted/capped/partial semantic
    # pass (budget cap, provider outage, unparseable batch) must pass
    # semantic_rows_considered explicitly.
    if semantic_rows_considered is None:
        semantic_rows_considered = 10 - len(quarantined) if semantic_iterations else 0

    return ScanResult(
        table_name="test_table",
        source_id="s",
        check_results=check_results or [],
        semantic_flags=semantic_flags or [],
        verified_flags=verified_flags or [],
        flagged_row_indices=sorted(all_flagged),
        semantic_iterations=semantic_iterations,
        quarantined_row_indices=quarantined,
        semantic_rows_considered=semantic_rows_considered,
    )


def test_perfect_data_scores_100_across_the_board():
    """Every metric actually ran (its check found nothing) — genuinely
    clean, not merely unevaluated. See test_unevaluated_metric_is_na_not_100
    below for the "never ran at all" case this used to be confused with."""
    checks = [_clean_check(m) for m in MetricCategory]
    scan = _scan_result(check_results=checks, semantic_iterations=1)
    card = compute_scorecard(scan, _table(10))
    assert card.overall_score == 100.0
    assert all(m.score == 100.0 and m.evaluated for m in card.metric_scores)


def test_unevaluated_metric_is_na_not_100():
    """The bug this replaces: a metric with no applicable check used to
    silently default to a perfect 100.0, indistinguishable from
    genuinely clean data. It must now report score=None, evaluated=False,
    and be excluded from the weighted overall average entirely."""
    scan = _scan_result()  # no check_results, no semantic activity at all
    card = compute_scorecard(scan, _table(10))
    for m in card.metric_scores:
        assert m.evaluated is False
        assert m.score is None
    # Nothing was evaluated at all -> the one legitimate 100.0 default.
    assert card.overall_score == 100.0


def _injection_check(flagged: list[int], total_rows: int = 10) -> CheckResult:
    return CheckResult(
        check_name="prompt_injection_check",
        metric=MetricCategory.ACCURACY,
        flagged_row_indices=flagged,
        total_rows_evaluated=total_rows,
        detail="matched instruction_override",
    )


def test_injection_check_flags_never_become_a_finding_or_touch_any_score():
    """The Quarantine Model's scoring-side requirement: a row the
    prompt-injection backstop flags must contribute ZERO weight to
    ACCURACY (or anything else) — not a diluted amount, not reduced
    weight, none. Every other metric is clean here, so if the
    injection flag leaked into scoring at all, ACCURACY (or
    overall_score) would drop below 100."""
    checks = [_clean_check(m) for m in MetricCategory if m != MetricCategory.ACCURACY]
    checks.append(_injection_check(flagged=[2, 5]))
    scan = _scan_result(check_results=checks)

    card = compute_scorecard(scan, _table(10))
    accuracy = next(m for m in card.metric_scores if m.metric == MetricCategory.ACCURACY)
    # A clean run of ONLY the injection check must not count as
    # "ACCURACY evaluated" either — it's a security check, not a
    # quality one, so with no semantic pass having run, ACCURACY has
    # no real quality signal at all.
    assert accuracy.evaluated is False
    assert accuracy.score is None
    assert accuracy.flagged_weight == 0.0


def test_injection_check_running_alone_does_not_make_accuracy_evaluated():
    """Companion to the above from the other direction: previously,
    ANY deterministic check under a metric category — including this
    one — made that metric read as evaluated. That's no longer true
    for prompt_injection_check specifically, since it's excluded from
    evaluated_metrics by check_name, same convention main.py already
    uses for referential_integrity_check's cross-table handling."""
    checks = [_clean_check(m) for m in MetricCategory if m != MetricCategory.ACCURACY]
    checks.append(_injection_check(flagged=[]))  # clean run, no hostile rows found
    scan = _scan_result(check_results=checks, semantic_iterations=0)

    card = compute_scorecard(scan, _table(10))
    accuracy = next(m for m in card.metric_scores if m.metric == MetricCategory.ACCURACY)
    assert accuracy.evaluated is False


def test_injection_check_does_not_block_accuracy_evaluation_when_semantic_layer_also_ran():
    """ACCURACY still reads as evaluated when the semantic layer itself
    ran a batch — the injection check's exclusion only removes IT as a
    source of evaluated-ness, it doesn't suppress the independent
    semantic-iterations signal."""
    checks = [_clean_check(m) for m in MetricCategory if m != MetricCategory.ACCURACY]
    checks.append(_injection_check(flagged=[3]))
    scan = _scan_result(check_results=checks, semantic_iterations=1)

    card = compute_scorecard(scan, _table(10))
    accuracy = next(m for m in card.metric_scores if m.metric == MetricCategory.ACCURACY)
    assert accuracy.evaluated is True
    assert accuracy.flagged_weight == 0.0  # the injection flag itself still never counts


def test_quarantined_rows_are_excluded_from_the_accuracy_denominator_not_counted_as_clean():
    """Bug 3 (see module docstring): a quarantined row must not count
    as "evaluated and clean" in ACCURACY's own denominator just
    because it contributes zero flagged_weight. 10 rows total, 4
    quarantined (never sampled by the semantic layer at all), 1 of the
    remaining 6 confirmed as a genuine semantic flag. If the
    denominator were still the full 10 rows, this would score
    100*(1-1/10)=90.0 — a real-looking number that quietly credits the
    4 unreasoned rows as clean. The lone flag is UNVERIFIED (no
    verifier configured in this test), so it weighs 0.5 (see
    _UNVERIFIED_SEMANTIC_WEIGHT) — the correct denominator is the 6
    rows actually eligible for semantic review: 100*(1-0.5/6)=91.7,
    not the 100*(1-0.5/10)=95.0 the unfixed denominator would give."""
    table = _table(10)
    flag = SemanticFlag(row_index=5, reason="looks off", confidence=0.9)
    scan = _scan_result(semantic_flags=[flag], quarantined_row_indices=[0, 1, 2, 3])

    card = compute_scorecard(scan, table)
    accuracy = next(m for m in card.metric_scores if m.metric == MetricCategory.ACCURACY)
    assert accuracy.total_rows == 6  # 10 - 4 quarantined, not the raw 10
    assert accuracy.score == 91.7


def test_deterministic_flag_reduces_its_metric_score():
    """COMPLETENESS is computed directly from the table's own blank
    cells (see Bug 4 in scorer.py's module docstring), not from a
    check's flagged_row_indices — so, unlike every other metric here,
    the table itself must actually contain the blanks the test is
    about. The CheckResult below only serves to mark COMPLETENESS as
    "evaluated" (mirroring what a real NullCheck run would report for
    the same table), same convention _clean_check exists for."""
    table = CanonicalTable(
        tenant_id="t", source_id="s", table_name="test_table",
        columns=[ColumnSchema(name="dummy", type=ColumnType.STRING)],
        rows=[{"dummy": "" if i < 2 else "x"} for i in range(10)],
    )
    check = CheckResult(
        check_name="null_check",
        metric=MetricCategory.COMPLETENESS,
        flagged_row_indices=[0, 1],
        total_rows_evaluated=10,
        detail="2 missing",
    )
    scan = _scan_result(check_results=[check, _clean_check(MetricCategory.VALIDITY)])
    card = compute_scorecard(scan, table)

    completeness = next(m for m in card.metric_scores if m.metric == MetricCategory.COMPLETENESS)
    assert completeness.score == 80.0  # unweighted fallback: 1 - 2/10 blank cells = 0.8 -> 80

    validity = next(m for m in card.metric_scores if m.metric == MetricCategory.VALIDITY)
    assert validity.evaluated is True
    assert validity.score == 100.0


def test_row_flagged_by_two_checks_same_metric_not_double_counted():
    check1 = CheckResult(
        check_name="check1", metric=MetricCategory.VALIDITY,
        flagged_row_indices=[0], total_rows_evaluated=10, detail="",
    )
    check2 = CheckResult(
        check_name="check2", metric=MetricCategory.VALIDITY,
        flagged_row_indices=[0], total_rows_evaluated=10, detail="",
    )
    scan = _scan_result(check_results=[check1, check2])
    card = compute_scorecard(scan, _table(10))

    validity = next(m for m in card.metric_scores if m.metric == MetricCategory.VALIDITY)
    assert validity.flagged_weight == 1.0  # not 2.0 — max(), not sum()
    assert validity.score == 90.0


def test_confirmed_semantic_flag_counts_full_weight():
    flag = SemanticFlag(row_index=0, reason="x", confidence=0.9)
    verified = VerifiedFlag(
        row_index=0, reason="x", original_confidence=0.9,
        label=VerificationLabel.CONFIRMED, verifier_notes="checked",
    )
    scan = _scan_result(semantic_flags=[flag], verified_flags=[verified])
    card = compute_scorecard(scan, _table(10))

    accuracy = next(m for m in card.metric_scores if m.metric == MetricCategory.ACCURACY)
    assert accuracy.flagged_weight == 1.0
    assert accuracy.score == 90.0


def test_needs_review_semantic_flag_counts_half_weight():
    flag = SemanticFlag(row_index=0, reason="x", confidence=0.5)
    verified = VerifiedFlag(
        row_index=0, reason="x", original_confidence=0.5,
        label=VerificationLabel.NEEDS_REVIEW, verifier_notes="unclear",
    )
    scan = _scan_result(semantic_flags=[flag], verified_flags=[verified])
    card = compute_scorecard(scan, _table(10))

    accuracy = next(m for m in card.metric_scores if m.metric == MetricCategory.ACCURACY)
    assert accuracy.flagged_weight == 0.5
    assert accuracy.score == 95.0


def test_rejected_semantic_flag_contributes_nothing():
    flag = SemanticFlag(row_index=0, reason="x", confidence=0.8)
    verified = VerifiedFlag(
        row_index=0, reason="x", original_confidence=0.8,
        label=VerificationLabel.REJECTED, verifier_notes="actually fine",
    )
    scan = _scan_result(semantic_flags=[flag], verified_flags=[verified])
    card = compute_scorecard(scan, _table(10))

    accuracy = next(m for m in card.metric_scores if m.metric == MetricCategory.ACCURACY)
    assert accuracy.flagged_weight == 0.0
    assert accuracy.score == 100.0


def test_unverified_semantic_flag_defaults_to_half_weight():
    flag = SemanticFlag(row_index=0, reason="x", confidence=0.95)
    scan = _scan_result(semantic_flags=[flag])
    card = compute_scorecard(scan, _table(10))

    accuracy = next(m for m in card.metric_scores if m.metric == MetricCategory.ACCURACY)
    assert accuracy.flagged_weight == 0.5


def test_custom_metric_weights_change_overall_score():
    # Every OTHER metric evaluated-and-clean (100.0) so the custom
    # weight on completeness has other evaluated metrics to outweigh —
    # with only one metric evaluated at all, a weight on it can't
    # change anything relative to itself. As in
    # test_deterministic_flag_reduces_its_metric_score above, the table
    # itself must contain the blanks — COMPLETENESS reads them directly.
    other_metrics = [m for m in MetricCategory if m != MetricCategory.COMPLETENESS]
    table = CanonicalTable(
        tenant_id="t", source_id="s", table_name="test_table",
        columns=[ColumnSchema(name="dummy", type=ColumnType.STRING)],
        rows=[{"dummy": "" if i < 5 else "x"} for i in range(10)],
    )
    check = CheckResult(
        check_name="null_check", metric=MetricCategory.COMPLETENESS,
        flagged_row_indices=[0, 1, 2, 3, 4], total_rows_evaluated=10, detail="",
    )
    scan = _scan_result(
        check_results=[check, *[_clean_check(m) for m in other_metrics]],
        semantic_iterations=1,
    )

    equal_weights = compute_scorecard(scan, table)
    heavy_completeness = compute_scorecard(
        scan, table, metric_weights={MetricCategory.COMPLETENESS: 10.0}
    )
    assert heavy_completeness.overall_score < equal_weights.overall_score


def test_zero_total_rows_does_not_crash_and_scores_100():
    scan = _scan_result()
    card = compute_scorecard(scan, _table(0))
    assert card.overall_score == 100.0


def test_score_never_goes_negative_even_if_flags_exceed_rows():
    check = CheckResult(
        check_name="x", metric=MetricCategory.VALIDITY,
        flagged_row_indices=[0, 1, 2], total_rows_evaluated=2, detail="",
    )
    scan = _scan_result(check_results=[check])
    card = compute_scorecard(scan, _table(2))
    validity = next(m for m in card.metric_scores if m.metric == MetricCategory.VALIDITY)
    assert validity.score == 0.0


# --- Field-criticality weighting ---------------------------------------

def _completeness_table() -> CanonicalTable:
    return CanonicalTable(
        tenant_id="t", source_id="s", table_name="leads",
        columns=[
            ColumnSchema(name="email", type=ColumnType.STRING, nullable=False),  # required
            ColumnSchema(name="notes", type=ColumnType.STRING, nullable=True),   # optional
        ],
        rows=[{"email": "a@x.com", "notes": "x"} for _ in range(10)],
    )


def test_required_column_blank_costs_score_but_non_required_blank_does_not():
    """Bug 4's replacement for the old "2x vs 1x weight" distinction:
    requiredness is now config-driven (`required_columns`), and a blank
    in a column NOT named there costs the score NOTHING at all — it's
    reported informationally (non_required_fill_rates) instead of at a
    reduced weight. The old version of this test fabricated a
    CheckResult's flagged_fields without the table actually containing
    a blank; that no longer has any effect, since COMPLETENESS reads
    the table's real cells directly (see Bug 4 in scorer.py)."""
    columns = _completeness_table().columns

    required_blank_table = CanonicalTable(
        tenant_id="t", source_id="s", table_name="leads", columns=columns,
        rows=[{"email": "" if i == 0 else "a@x.com", "notes": "x"} for i in range(10)],
    )
    optional_blank_table = CanonicalTable(
        tenant_id="t", source_id="s", table_name="leads", columns=columns,
        rows=[{"email": "a@x.com", "notes": "" if i == 0 else "x"} for i in range(10)],
    )
    check = _clean_check(MetricCategory.COMPLETENESS, total_rows=10)

    required_card = compute_scorecard(
        _scan_result(check_results=[check]), required_blank_table, required_columns=["email"]
    )
    optional_card = compute_scorecard(
        _scan_result(check_results=[check]), optional_blank_table, required_columns=["email"]
    )

    required_completeness = next(
        m for m in required_card.metric_scores if m.metric == MetricCategory.COMPLETENESS
    )
    optional_completeness = next(
        m for m in optional_card.metric_scores if m.metric == MetricCategory.COMPLETENESS
    )

    assert required_completeness.flagged_weight == 1.0  # 1 blank required cell
    assert required_completeness.score == 90.0  # 1 - 1/10

    assert optional_completeness.flagged_weight == 0.0  # the blank was in "notes" — not required
    assert optional_completeness.score == 100.0
    assert optional_card.non_required_fill_rates == {"notes": 90.0}  # 9 of 10 rows filled


def test_multiple_blank_required_cells_in_one_row_all_count_not_capped_at_one():
    """Cell-level scoring has no row-level max() cap the way the old
    Finding-weight machinery did — every blank required cell counts
    toward the fraction on its own, since the denominator is CELLS, not
    rows. A row missing both of 2 required columns costs twice what a
    row missing only 1 does."""
    table = CanonicalTable(
        tenant_id="t", source_id="s", table_name="leads",
        columns=[
            ColumnSchema(name="email", type=ColumnType.STRING),
            ColumnSchema(name="phone", type=ColumnType.STRING),
        ],
        rows=[
            {"email": "a@x.com", "phone": "555"},  # clean
            {"email": "", "phone": ""},              # both required cells blank
        ],
    )
    check = _clean_check(MetricCategory.COMPLETENESS, total_rows=2)
    card = compute_scorecard(
        _scan_result(check_results=[check]), table, required_columns=["email", "phone"]
    )
    completeness = next(m for m in card.metric_scores if m.metric == MetricCategory.COMPLETENESS)

    assert completeness.total_rows == 4  # 2 rows * 2 required columns — the real denominator, in cells
    assert completeness.flagged_weight == 2.0  # both of row 1's required cells, not capped at 1
    assert completeness.score == 50.0  # 1 - 2/4


def test_check_without_field_attribution_falls_back_to_flat_weight():
    """A check that doesn't populate flagged_fields (like
    DuplicateCheck) must behave exactly as it did before this feature
    existed — flat weight 1.0, not penalized or rewarded for missing
    attribution."""
    table = _completeness_table()
    check = CheckResult(
        check_name="duplicate_check", metric=MetricCategory.UNIQUENESS,
        flagged_row_indices=[0, 1], total_rows_evaluated=10, detail="",
    )
    card = compute_scorecard(_scan_result(check_results=[check]), table)
    uniqueness = next(m for m in card.metric_scores if m.metric == MetricCategory.UNIQUENESS)
    assert uniqueness.flagged_weight == 2.0  # 2 rows * flat weight 1.0 each
    assert uniqueness.score == 80.0


def test_unknown_field_name_defaults_to_optional_weight_not_a_crash():
    table = _completeness_table()
    check = CheckResult(
        check_name="weird_check", metric=MetricCategory.VALIDITY,
        flagged_row_indices=[0], total_rows_evaluated=10, detail="",
        flagged_fields={0: ["nonexistent_field"]},
    )
    card = compute_scorecard(_scan_result(check_results=[check]), table)
    validity = next(m for m in card.metric_scores if m.metric == MetricCategory.VALIDITY)
    assert validity.flagged_weight == 1.0


def test_criticality_weighting_end_to_end_with_real_null_check():
    """Integration check: run the actual NullCheck (not a hand-built
    CheckResult) through the real scorer, confirming the two layers
    actually connect correctly.

    NullCheck itself is unaffected by Bug 4 — it still flags a blank in
    ANY column (see its own docstring for the earlier nullable-from-
    data circularity this already fixed): row 1 (blank "email") AND
    row 2 (blank "notes") are both flagged here. What Bug 4 changed is
    how the SCORER weighs those two flags — no longer via
    ColumnSchema.nullable (itself circular for the CSV connector), but
    via the caller's own `required_columns` config, at the cell level."""
    from app.checks.completeness import NullCheck

    table = CanonicalTable(
        tenant_id="t", source_id="s", table_name="leads",
        columns=[
            ColumnSchema(name="email", type=ColumnType.STRING, nullable=False),
            ColumnSchema(name="notes", type=ColumnType.STRING, nullable=True),
        ],
        rows=[
            {"email": "a@x.com", "notes": "ok"},   # clean
            {"email": "", "notes": "ok"},            # email blank
            {"email": "b@x.com", "notes": ""},       # notes blank
        ],
    )
    result = NullCheck().run(table)
    assert result.flagged_row_indices == [1, 2]

    scan = _scan_result(check_results=[result])

    # With "email" configured as the required column: only its 1 blank
    # cell (out of 3 required cells) counts toward the score.
    configured = compute_scorecard(scan, table, required_columns=["email"])
    completeness = next(m for m in configured.metric_scores if m.metric == MetricCategory.COMPLETENESS)
    assert completeness.flagged_weight == 1.0  # 1 blank required cell, not the old row-weight sum of 3.0
    assert completeness.total_rows == 3  # 3 rows * 1 required column — the real denominator, in cells
    assert completeness.score == 66.7  # 1 - 1/3
    # "notes" isn't required — its fill rate is reported, never scored.
    assert configured.non_required_fill_rates == {"notes": 66.7}  # 2 of 3 rows have a non-blank notes

    # Without any required_columns configured, completeness falls back
    # to an unweighted overall fill rate — every blank cell counts
    # equally regardless of which column it's in.
    unconfigured = compute_scorecard(scan, table)
    fallback = next(m for m in unconfigured.metric_scores if m.metric == MetricCategory.COMPLETENESS)
    assert unconfigured.completeness_mode == "no_required_fields_configured"
    assert fallback.flagged_weight == 2.0  # both blank cells (row 1's email, row 2's notes)
    assert fallback.total_rows == 6  # 3 rows * 2 columns
    assert fallback.score == 66.7  # 1 - 2/6


def test_overall_score_is_capped_near_the_worst_metric_not_diluted_by_clean_ones():
    """Reproduced bug: a table where 40% of a REQUIRED field is blank
    scored badly on completeness in isolation (correctly alarming) but
    a much healthier overall_score — a straight 5-way average let four
    untouched, trivially-clean metrics absorb the one real problem.
    overall_score must now stay within _OVERALL_CRITICAL_CAP_ALLOWANCE
    (20.0) of the worst evaluated metric, so a severe single-dimension
    problem can't be diluted away by unrelated clean dimensions. Passes
    `required_columns=["email"]` so completeness scores this the
    units-correct way (see Bug 4) instead of falling back to the
    unweighted-overall-fill-rate default, which would dilute the 40%
    figure across the always-filled "name" column too and understate
    how bad this specific configured requirement actually is."""
    n = 100
    columns = [
        ColumnSchema(name="email", type=ColumnType.STRING, nullable=False, semantic_hint="email"),
        ColumnSchema(name="name", type=ColumnType.STRING, nullable=False),
    ]
    rows = [
        {"email": "" if i < 40 else f"user{i}@x.com", "name": f"Person {i}"} for i in range(n)
    ]
    table = CanonicalTable(tenant_id="t", source_id="s", table_name="t", columns=columns, rows=rows)

    scan = GeneratorAgent().run(table)
    card = compute_scorecard(scan, table, required_columns=["email"])

    completeness = next(m for m in card.metric_scores if m.metric == MetricCategory.COMPLETENESS)
    assert completeness.score == 60.0  # 1 - 40/100 required "email" cells blank

    # Old (buggy) behavior would have averaged this away across four
    # untouched, trivially-clean metrics. It must now stay within the
    # cap allowance of the worst metric.
    assert card.overall_score <= completeness.score + 20.0


def test_full_pipeline_frozen_baseline_catches_scoring_drift():
    """Anti-regression guard for the Finding-model refactor's central
    claim: "the scorer now builds Finding[] internally, but produces
    unchanged legacy ScoreCard/MetricScore output." That claim was
    verified manually (a live run_schema_eval.py comparison recomputed
    the same aggregate_score before and after the refactor), but
    nothing pinned it as an automated check — so a future change to
    the Finding conversion, severity mapping, or scoring math could
    silently drift and nothing in `pytest -q` would catch it.

    This runs the real GeneratorAgent (no LLM — deterministic checks
    only, so it's fast and needs no API keys) end-to-end against a
    small fixed table with one planted issue per check
    (null/duplicate/format-validity; outlier stays clean by design so
    its metric is pinned at 100). prompt_injection_check also stays
    clean here (no hostile payload planted), but — since the Quarantine
    Model fix (see version.py's DETECTION_VERSION/SCORING_VERSION 1.4.0/
    1.3.0 notes) — a clean run of that check no longer counts as
    "ACCURACY evaluated" the way it used to, since it's a security
    check, not a quality one; with no LLM configured here, ACCURACY has
    no evaluated signal at all and is correctly excluded (evaluated=False)
    rather than padded with a clean 100. That's WHY overall_score is
    80.0 here rather than a straight 5-way average: the 100 ACCURACY
    used to contribute is gone from the average entirely, not merely
    replaced; COMPLETENESS is 90.0 (as of Bug 4 — config-driven
    requiredness with the units fix, see scorer.py's module docstring),
    not the old nullable-weighted 60.0 — no `required_columns` is
    configured in this test, so it falls back to an unweighted overall
    fill rate (1 blank cell out of 5 rows * 2 columns = 10 cells),
    rather than the old row-weighted formula that graded "email" as
    2x-critical purely because ColumnSchema.nullable said so; and
    UNIQUENESS is 80.0, not the old 60.0 — DuplicateCheck now flags all
    but the earliest-seen row in a duplicate group (see uniqueness.py's
    all-but-one fix), so only row 2 counts against it, not row 0 too.
    Then asserts the exact scorecard values that dataset produces
    today. If a future change to the Finding <-> scorer boundary alters
    any of these numbers, this test fails and says so explicitly — it
    does not require the old pre-refactor code to still exist to be
    useful as a tripwire going forward."""
    columns = [
        ColumnSchema(name="email", type=ColumnType.STRING, nullable=False, semantic_hint="email"),
        ColumnSchema(name="amount", type=ColumnType.INTEGER, nullable=True),
    ]
    rows = [
        {"email": "a@example.com", "amount": 100},
        {"email": "", "amount": 200},                      # null check -> row 1
        {"email": "a@example.com", "amount": 100},          # duplicate of row 0 -> row 2 only (0 is "the original")
        {"email": "not-an-email", "amount": 150},           # format validity -> row 3
        {"email": "d@example.com", "amount": 9999999},      # deliberately not flagged as outlier
    ]
    table = CanonicalTable(
        tenant_id="t", source_id="s", table_name="frozen_baseline", columns=columns, rows=rows,
    )

    scan = GeneratorAgent().run(table)
    # Row 0 is "the original" of its duplicate group (see uniqueness.py's
    # all-but-one fix) and is no longer flagged just for being duplicated.
    assert scan.flagged_row_indices == [1, 2, 3]

    card = compute_scorecard(scan, table)
    accuracy_score = next(m for m in card.metric_scores if m.metric == MetricCategory.ACCURACY)
    assert accuracy_score.evaluated is False
    assert accuracy_score.score is None

    completeness_score = next(m for m in card.metric_scores if m.metric == MetricCategory.COMPLETENESS)
    assert completeness_score.score == 90.0  # unweighted fallback: 1 blank cell / 10 total cells

    assert card.overall_score == 80.0

    by_metric = {m.metric.value: m for m in card.metric_scores}
    assert by_metric["completeness"].score == 90.0
    assert by_metric["uniqueness"].score == 80.0  # only row 2 flagged, not row 0 too
    assert by_metric["validity"].score == 60.0
    assert by_metric["consistency"].score == 100.0
    assert by_metric["accuracy"].score is None  # unevaluated, not a padded 100 — see docstring above


def test_probe_worse_data_no_longer_scores_higher_with_required_columns_configured():
    """The original probe this whole Bug 4 investigation traced back
    to: two otherwise-identical tables, one with a column 40% blank,
    one with the SAME column 60% blank. Under the old
    ColumnSchema.nullable-derived weighting, the 40%-blank column was
    inferred "required" (2x weight) while the 60%-blank column crossed
    the CSV connector's 50%-blank threshold and was inferred "optional"
    (1x weight) — so the WORSE (60%-blank) table scored completeness
    40.0 while the better (40%-blank) table scored 20.0, an inversion.
    With "email" explicitly configured as required, both tables are
    graded by the same units-correct cell fraction regardless of their
    own blank ratio, so the ordering is now correct: more blank ->
    lower score, always."""
    from app.checks.completeness import NullCheck

    def _table_with_blank_ratio(blank_ratio: float) -> CanonicalTable:
        n = 100
        blank_count = int(n * blank_ratio)
        columns = [ColumnSchema(name="email", type=ColumnType.STRING)]
        rows = [{"email": "" if i < blank_count else f"user{i}@x.com"} for i in range(n)]
        return CanonicalTable(tenant_id="t", source_id="s", table_name="t", columns=columns, rows=rows)

    forty_pct_blank = _table_with_blank_ratio(0.40)
    sixty_pct_blank = _table_with_blank_ratio(0.60)

    forty_result = NullCheck().run(forty_pct_blank)
    sixty_result = NullCheck().run(sixty_pct_blank)

    forty_scan = _scan_result(check_results=[forty_result])
    sixty_scan = _scan_result(check_results=[sixty_result])

    forty_card = compute_scorecard(forty_scan, forty_pct_blank, required_columns=["email"])
    sixty_card = compute_scorecard(sixty_scan, sixty_pct_blank, required_columns=["email"])

    forty_completeness = next(m for m in forty_card.metric_scores if m.metric == MetricCategory.COMPLETENESS)
    sixty_completeness = next(m for m in sixty_card.metric_scores if m.metric == MetricCategory.COMPLETENESS)

    assert forty_completeness.score == 60.0  # 1 - 40/100
    assert sixty_completeness.score == 40.0  # 1 - 60/100
    assert sixty_completeness.score < forty_completeness.score  # worse data scores lower, not higher
