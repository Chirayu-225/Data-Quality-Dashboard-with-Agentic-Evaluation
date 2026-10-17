import pytest

from app.agent.llm_clients import LLMClient, LLMRequestTooLargeError
from app.agent.semantic_reasoning import _MIN_SAMPLE_SIZE, SemanticReasoner
from app.canonical.models import CanonicalTable, ColumnSchema, ColumnType


class _FakeLLM(LLMClient):
    """Returns a canned response instead of calling a real API — lets
    us test parsing and integration without any network access or
    real keys."""

    def __init__(self, canned_response: str):
        self.canned_response = canned_response
        self.last_system_prompt: str | None = None
        self.last_user_prompt: str | None = None

    def complete(self, system_prompt: str, user_prompt: str) -> str:
        self.last_system_prompt = system_prompt
        self.last_user_prompt = user_prompt
        return self.canned_response


class _FakeStatusError(Exception):
    """Stands in for a real SDK exception (e.g. groq.APIStatusError) —
    only `.status_code` matters to the code under test."""

    def __init__(self, message: str, status_code: int):
        super().__init__(message)
        self.status_code = status_code


class _TooLargeUntilSmallLLM(LLMClient):
    """Fails with a 413 for any batch bigger than `max_rows_ok`,
    succeeds otherwise — used to test SemanticReasoner's
    shrink-then-resubmit behavior without a real provider."""

    def __init__(self, max_rows_ok: int):
        self.max_rows_ok = max_rows_ok
        self.rows_per_call: list[int] = []  # how many rows each call actually sent

    def complete(self, system_prompt: str, user_prompt: str) -> str:
        rows_in_prompt = user_prompt.count("] row ")
        self.rows_per_call.append(rows_in_prompt)
        if rows_in_prompt > self.max_rows_ok:
            raise _FakeStatusError("Request too large", status_code=413)
        return '{"flags": []}'


def _table() -> CanonicalTable:
    return CanonicalTable(
        tenant_id="t",
        source_id="s",
        table_name="leads",
        columns=[
            ColumnSchema(name="company", type=ColumnType.STRING),
            ColumnSchema(name="industry", type=ColumnType.STRING),
        ],
        rows=[
            {"company": "Acme Corp", "industry": "Technology"},
            {"company": "Joe's Bakery", "industry": "Technology"},  # semantically odd
        ],
    )


def test_parses_well_formed_json_response():
    llm = _FakeLLM(
        '{"flags": [{"row_index": 1, "reason": "bakery listed as tech", "confidence": 0.8}]}'
    )
    result = SemanticReasoner(llm).run(_table())
    assert len(result.flags) == 1
    assert result.flags[0].row_index == 1
    assert result.flags[0].confidence == 0.8


def test_handles_markdown_fenced_json():
    llm = _FakeLLM('```json\n{"flags": [{"row_index": 1, "reason": "x", "confidence": 0.5}]}\n```')
    result = SemanticReasoner(llm).run(_table())
    assert len(result.flags) == 1


def test_empty_flags_when_nothing_wrong():
    llm = _FakeLLM('{"flags": []}')
    result = SemanticReasoner(llm).run(_table())
    assert result.flags == []


def test_fails_safe_on_malformed_json():
    llm = _FakeLLM("this is not json at all, sorry")
    result = SemanticReasoner(llm).run(_table())
    assert result.flags == []  # no crash, just no flags


def test_malformed_json_is_flagged_as_a_parse_failure_not_silently_clean():
    """Reproduced bug: a parse failure returned zero flags, and those
    rows were then recorded as covered — indistinguishable from "the
    model looked and found nothing wrong." parse_failed=True is what
    lets a caller (GeneratorAgent) treat this as a coverage gap
    instead of silently counting it as a clean result."""
    llm = _FakeLLM("this is not json at all, sorry")
    result = SemanticReasoner(llm).run(_table())
    assert result.parse_failed is True


def test_well_formed_json_is_not_flagged_as_a_parse_failure():
    llm = _FakeLLM('{"flags": []}')
    result = SemanticReasoner(llm).run(_table())
    assert result.parse_failed is False


def test_fails_safe_on_missing_required_fields():
    llm = _FakeLLM('{"flags": [{"row_index": 1}]}')  # missing "reason"
    result = SemanticReasoner(llm).run(_table())
    assert result.flags == []


def test_row_data_is_framed_as_untrusted_in_prompt():
    """Guards the prompt-injection mitigation — this should never
    silently regress into treating row data as trusted."""
    llm = _FakeLLM('{"flags": []}')
    SemanticReasoner(llm).run(_table())
    assert "UNTRUSTED DATA" in llm.last_user_prompt


def test_system_prompt_does_not_tell_the_model_to_flag_routine_business_phrasing():
    """Alignment fix: the deterministic PromptInjectionCheck treats
    "pre-approved", "already reviewed", "verified by", and
    "compliance team"/"compliance notice" as LOW-tier phrasing — worth
    a low-severity report, but not quarantine-worthy. The semantic
    prompt must not instruct the LLM to undo that tiering by treating
    the same phrases as high-confidence manipulation signals. Guards
    against the exact regression reproduced earlier this session."""
    llm = _FakeLLM('{"flags": []}')
    SemanticReasoner(llm).run(_table())
    system_prompt = llm.last_system_prompt

    for phrase in ("pre-approved", "already reviewed", "verified by", "compliance team"):
        assert phrase in system_prompt  # still discussed...
    # ...but explicitly carved out, not folded into the manipulation-signal list.
    assert "is NOT on its own a manipulation signal" in system_prompt
    assert "do not treat it as evidence of manipulation" in system_prompt.lower()


def test_system_prompt_still_flags_genuine_injection_signals_with_high_confidence():
    """The carve-out for routine business phrasing must not water down
    the genuinely high-confidence signals it sits beside — instruction
    overrides, fake system messages, and cross-row targeting are still
    called out as a strong, independent signal worth a high-confidence
    flag."""
    llm = _FakeLLM('{"flags": []}')
    SemanticReasoner(llm).run(_table())
    system_prompt = llm.last_system_prompt

    assert "ignore previous" in system_prompt.lower()
    assert "strong, independent signal of manipulation" in system_prompt
    assert "flag the row with high confidence" in system_prompt.lower()
    assert "never instructions" in llm.last_system_prompt.lower() or "untrusted" in llm.last_system_prompt.lower()


def test_custom_instruction_is_included_when_provided():
    llm = _FakeLLM('{"flags": []}')
    SemanticReasoner(llm).run(_table(), custom_instruction="flag any joke company names")
    assert "flag any joke company names" in llm.last_user_prompt


def test_sample_size_limits_rows_sent():
    big_table = CanonicalTable(
        tenant_id="t",
        source_id="s",
        table_name="big",
        columns=[ColumnSchema(name="x", type=ColumnType.STRING)],
        rows=[{"x": str(i)} for i in range(100)],
    )
    llm = _FakeLLM('{"flags": []}')
    result = SemanticReasoner(llm, sample_size=10).run(big_table)
    assert result.rows_sampled == 10


def test_sampling_is_deterministic_for_the_same_table_content():
    """Reproduced bug: two scans of the identical file could sample
    different rows (unseeded random.sample) and therefore return
    different flags and a different score for identical input.
    Sampling is now seeded from the table's own content (see
    _content_seed), so a fresh SemanticReasoner instance given the
    SAME table always draws the SAME sample — this replaces the old
    "eventually both indices show up" randomness check, since genuine
    randomness is no longer the intended behavior for repeat scans of
    one file."""
    llm = _FakeLLM('{"flags": []}')
    results = [SemanticReasoner(llm, sample_size=1).run(_table()).sampled_indices for _ in range(10)]
    assert all(r == results[0] for r in results)
    assert results[0][0] in (0, 1)


def test_sampling_differs_for_genuinely_different_table_content():
    """The seed is derived from the table's actual row content, not
    just its shape — two tables with different data (even same name,
    same row count) must not be locked into an identical sample
    purely because determinism was added; only a true re-scan of the
    SAME file should reproduce the SAME sample."""
    llm = _FakeLLM('{"flags": []}')
    table_a = CanonicalTable(
        tenant_id="t", source_id="s", table_name="big",
        columns=[ColumnSchema(name="x", type=ColumnType.STRING)],
        rows=[{"x": str(i)} for i in range(50)],
    )
    table_b = CanonicalTable(
        tenant_id="t", source_id="s", table_name="big",
        columns=[ColumnSchema(name="x", type=ColumnType.STRING)],
        rows=[{"x": str(i + 1000)} for i in range(50)],  # same shape, different content
    )
    sample_a = SemanticReasoner(llm, sample_size=5).run(table_a).sampled_indices
    sample_b = SemanticReasoner(llm, sample_size=5).run(table_b).sampled_indices
    assert sample_a != sample_b


def test_exclude_indices_skips_already_sampled_rows():
    """This is what makes the iteration loop's 'fresh batch' behavior
    possible — a repeat call with the previous cycle's indices
    excluded must not resample them."""
    big_table = CanonicalTable(
        tenant_id="t",
        source_id="s",
        table_name="big",
        columns=[ColumnSchema(name="x", type=ColumnType.STRING)],
        rows=[{"x": str(i)} for i in range(5)],
    )
    llm = _FakeLLM('{"flags": []}')
    result = SemanticReasoner(llm, sample_size=3).run(big_table, exclude_indices={0, 1, 2})
    assert result.sampled_indices == [3, 4]
    assert result.rows_sampled == 2


def test_exclude_indices_can_exhaust_the_table():
    llm = _FakeLLM('{"flags": []}')
    result = SemanticReasoner(llm).run(_table(), exclude_indices={0, 1})
    assert result.rows_sampled == 0
    assert result.sampled_indices == []


def _big_table(n: int) -> CanonicalTable:
    return CanonicalTable(
        tenant_id="t",
        source_id="s",
        table_name="big",
        columns=[ColumnSchema(name="x", type=ColumnType.STRING)],
        rows=[{"x": str(i)} for i in range(n)],
    )


def test_shrinks_batch_on_413_until_it_succeeds():
    """A batch too large for one request should be halved and
    resubmitted — not retried unchanged, and not given up on — until
    it fits, and the reasoner should remember the size that worked for
    later batches in the same scan."""
    llm = _TooLargeUntilSmallLLM(max_rows_ok=20)
    reasoner = SemanticReasoner(llm, sample_size=100)

    result = reasoner.run(_big_table(100))

    assert result.rows_sampled <= 20  # ended up at or under the size that actually works
    assert len(llm.rows_per_call) > 1  # it actually shrank and retried, not one-shot
    assert llm.rows_per_call[0] == 100  # first attempt was the full requested size
    assert llm.rows_per_call[-1] == result.rows_sampled  # last attempt is what succeeded
    # Persisted for next time — a later batch in the same scan won't
    # have to rediscover the same 413 from scratch.
    assert reasoner.sample_size == result.rows_sampled


def test_gives_up_when_even_the_floor_size_is_too_large():
    """If shrinking all the way down to _MIN_SAMPLE_SIZE still gets
    rejected as too large, that's a genuinely different problem
    (e.g. one row's own content is huge) that shrinking further can't
    fix — this should propagate as LLMRequestTooLargeError rather than
    loop forever or silently return nothing."""
    llm = _TooLargeUntilSmallLLM(max_rows_ok=1)  # even the floor size is too big
    reasoner = SemanticReasoner(llm, sample_size=100)

    with pytest.raises(LLMRequestTooLargeError):
        reasoner.run(_big_table(100))

    # It should have actually shrunk all the way down before giving up,
    # not failed immediately on the first attempt.
    assert min(llm.rows_per_call) == _MIN_SAMPLE_SIZE


def test_normal_batch_within_limit_is_unaffected():
    """A batch that never hits a 413 should behave exactly as before —
    one call, no shrinking, sample_size unchanged."""
    llm = _TooLargeUntilSmallLLM(max_rows_ok=50)
    reasoner = SemanticReasoner(llm, sample_size=20)

    result = reasoner.run(_big_table(100))

    assert result.rows_sampled == 20
    assert len(llm.rows_per_call) == 1
    assert reasoner.sample_size == 20


def test_drops_a_flag_for_a_row_index_not_actually_in_this_batch():
    """A hallucinated or (adversarially) injected row_index that was
    never part of this batch's sample must never make it into the
    result — sample_size=1 makes the shown set deterministic (row 0
    or row 1, never both), so any OTHER index in the canned response
    is provably not something this batch actually sent."""
    llm = _FakeLLM('{"flags": [{"row_index": 0, "reason": "x", "confidence": 0.9}, '
                   '{"row_index": 1, "reason": "y", "confidence": 0.9}]}')
    result = SemanticReasoner(llm, sample_size=1).run(_table())
    assert len(result.sampled_indices) == 1
    shown = result.sampled_indices[0]
    # Only the flag matching the row actually shown survives — the
    # other one (for the row NOT sent this batch) is dropped.
    assert [f.row_index for f in result.flags] == [shown]


def test_drops_a_flag_for_a_row_index_outside_the_table_entirely():
    """Same protection against a fabricated out-of-range index (e.g. a
    row-content-injection payload shaped like {"row_index": 999, ...})
    — not just a real row that merely wasn't in this batch."""
    llm = _FakeLLM('{"flags": [{"row_index": 999, "reason": "fake", "confidence": 1.0}]}')
    result = SemanticReasoner(llm, sample_size=2).run(_table())
    assert result.flags == []
