from app.agent.cost_estimate import estimate_scan_cost
from app.canonical.models import CanonicalTable, ColumnSchema, ColumnType


def _table(n_rows: int) -> CanonicalTable:
    return CanonicalTable(
        tenant_id="t",
        source_id="s",
        table_name="leads",
        columns=[ColumnSchema(name="email", type=ColumnType.STRING, nullable=False)],
        rows=[{"email": f"user{i}@example.com"} for i in range(n_rows)],
    )


def test_estimate_is_zero_cost_for_a_table_over_the_row_limit_without_opt_in(monkeypatch):
    """Mirrors main.py's _llm_for_table gate (item 4) — the preview must
    never promise coverage a real scan wouldn't actually deliver for an
    over-limit table scanned without allow_large_semantic_scan."""
    monkeypatch.setattr("app.agent.cost_estimate.max_semantic_reasoning_rows", lambda: 10)
    estimate = estimate_scan_cost(_table(50))
    assert estimate.estimated_llm_calls == 0
    assert estimate.estimated_coverage_pct == 0.0
    assert estimate.estimated_cost_usd == 0.0
    assert "MAX_SEMANTIC_REASONING_ROWS" in estimate.note


def test_estimate_restores_full_coverage_with_the_opt_in_flag(monkeypatch):
    monkeypatch.setattr("app.agent.cost_estimate.max_semantic_reasoning_rows", lambda: 10)
    estimate = estimate_scan_cost(_table(50), allow_large_semantic_scan=True)
    assert estimate.estimated_llm_calls > 0
    assert estimate.estimated_coverage_pct == 100.0


def test_estimate_under_the_row_limit_is_unaffected(monkeypatch):
    monkeypatch.setattr("app.agent.cost_estimate.max_semantic_reasoning_rows", lambda: 1000)
    estimate = estimate_scan_cost(_table(50))
    assert estimate.estimated_llm_calls > 0
    assert estimate.estimated_coverage_pct == 100.0
