"""
Pre-scan cost/coverage estimate — shown to the user BEFORE they commit
to running a scan, rather than surprising them with the actual LLM
call count after the fact. Pure arithmetic over the same batching math
GeneratorAgent._run_semantic_with_iteration already uses (full-table
coverage by default, `_DEFAULT_PRODUCTION_SAMPLE_SIZE` rows per call,
capped by an optional `max_llm_calls`) — this module runs NO LLM
calls itself and doesn't need API keys configured to produce an
estimate.

Deliberately conservative about what it claims to know:
- Verifier calls are NOT included in the estimate. How many rows the
  generator flags — and therefore how many verification calls that
  requires — is exactly what running the scan determines; estimating
  it beforehand would mean guessing the scan's own outcome. The
  returned estimate says so explicitly rather than silently omitting
  it.
- The dollar figure is illustrative, not authoritative. Real
  per-token pricing varies by provider, model, and changes over time;
  hardcoding a rate that goes stale would be worse than not giving a
  number, so `_ILLUSTRATIVE_COST_PER_1K_TOKENS` is clearly labeled as
  a rough placeholder in both the code and the response, not fetched
  from any live pricing source.
"""

from __future__ import annotations

import math

from pydantic import BaseModel

from app.agent.generator import _DEFAULT_PRODUCTION_SAMPLE_SIZE
from app.agent.llm_clients import _estimate_tokens
from app.canonical.models import CanonicalTable
from app.config import max_semantic_reasoning_rows

# Rough, clearly-labeled placeholder — NOT live pricing. Picked as a
# round, conservative number in the ballpark of small hosted-model
# per-token rates, purely so the estimate has SOME illustrative dollar
# figure rather than none; always shown alongside a disclaimer.
_ILLUSTRATIVE_COST_PER_1K_TOKENS = 0.002

# Rough fixed overhead per call for the system prompt + schema
# description + instruction line that _build_user_prompt always adds
# on top of the sampled rows themselves (semantic_reasoning.py)  —
# estimated once, conservatively, rather than re-deriving the exact
# current prompt text here and coupling this module to it.
_FIXED_OVERHEAD_TOKENS_PER_CALL = 250


class ScanCostEstimate(BaseModel):
    table_name: str
    total_rows: int
    batch_size: int
    estimated_llm_calls: int
    estimated_coverage_pct: float
    estimated_input_tokens: int
    estimated_cost_usd: float
    note: str


def estimate_scan_cost(
    table: CanonicalTable,
    *,
    sample_size: int | None = None,
    max_llm_calls: int | None = None,
    allow_large_semantic_scan: bool = False,
) -> ScanCostEstimate:
    total_rows = len(table.rows)
    batch_size = sample_size or _DEFAULT_PRODUCTION_SAMPLE_SIZE

    if total_rows == 0:
        return ScanCostEstimate(
            table_name=table.table_name,
            total_rows=0,
            batch_size=batch_size,
            estimated_llm_calls=0,
            estimated_coverage_pct=100.0,
            estimated_input_tokens=0,
            estimated_cost_usd=0.0,
            note="Empty table — no semantic reasoning calls needed.",
        )

    # Mirrors main.py's _llm_for_table row-count gate — without this,
    # the preview would promise coverage the real scan wouldn't
    # actually deliver for an over-limit table scanned without the
    # opt-in flag.
    row_limit = max_semantic_reasoning_rows()
    if total_rows > row_limit and not allow_large_semantic_scan:
        return ScanCostEstimate(
            table_name=table.table_name,
            total_rows=total_rows,
            batch_size=batch_size,
            estimated_llm_calls=0,
            estimated_coverage_pct=0.0,
            estimated_input_tokens=0,
            estimated_cost_usd=0.0,
            note=f"{total_rows} rows is over the {row_limit}-row default for semantic "
            f"reasoning (MAX_SEMANTIC_REASONING_ROWS) — a real scan would run deterministic "
            f"checks only for this table (0 LLM calls) unless allow_large_semantic_scan=true "
            f"is passed to opt in to full coverage.",
        )

    batches_for_full_coverage = math.ceil(total_rows / batch_size)
    if max_llm_calls is not None:
        estimated_calls = min(batches_for_full_coverage, max_llm_calls)
        coverage_pct = round(min(100.0, estimated_calls * batch_size / total_rows * 100), 1)
    else:
        estimated_calls = batches_for_full_coverage
        coverage_pct = 100.0

    # Average a small sample of the table's own rows to approximate
    # per-row token cost, rather than assuming a fixed row size that
    # may not match this dataset's actual field lengths.
    sample_rows = table.rows[: min(20, total_rows)]
    avg_row_tokens = (
        sum(_estimate_tokens(str(row)) for row in sample_rows) / len(sample_rows)
        if sample_rows
        else 20
    )
    tokens_per_call = _FIXED_OVERHEAD_TOKENS_PER_CALL + avg_row_tokens * min(batch_size, total_rows)
    estimated_input_tokens = int(tokens_per_call * estimated_calls)
    estimated_cost_usd = round(estimated_input_tokens / 1000 * _ILLUSTRATIVE_COST_PER_1K_TOKENS, 4)

    note = (
        "Estimate only — actual batching can differ if a batch's real "
        "payload is rejected as too large and shrinks (see "
        "LLMRequestTooLargeError), and this does NOT include verifier "
        "calls, since how many rows get flagged (and therefore need "
        "verification) is exactly what running the scan determines. "
        "The dollar figure uses an illustrative placeholder rate, not "
        "live provider pricing — check your provider's current rates "
        "for an authoritative cost."
    )

    return ScanCostEstimate(
        table_name=table.table_name,
        total_rows=total_rows,
        batch_size=batch_size,
        estimated_llm_calls=estimated_calls,
        estimated_coverage_pct=coverage_pct,
        estimated_input_tokens=estimated_input_tokens,
        estimated_cost_usd=estimated_cost_usd,
        note=note,
    )
