import pytest

from app.agent.cross_table_reasoning import (
    CrossTableReasoner,
    CrossTableVerificationLabel,
    CrossTableVerifier,
    resolve_fk_pairs,
    run_cross_table_semantic_with_iteration,
)
from app.agent.llm_clients import LLMClient, LLMUnavailableError
from app.canonical.models import CanonicalTable, ColumnSchema, ColumnType


class _FakeLLM(LLMClient):
    def __init__(self, canned_response: str):
        self.canned_response = canned_response
        self.calls: list[tuple[str, str]] = []

    def complete(self, system_prompt: str, user_prompt: str) -> str:
        self.calls.append((system_prompt, user_prompt))
        return self.canned_response


class _AlwaysFailsLLM(LLMClient):
    def complete(self, system_prompt: str, user_prompt: str) -> str:
        raise RuntimeError("provider down")


def _accounts() -> CanonicalTable:
    return CanonicalTable(
        tenant_id="t",
        source_id="s",
        table_name="accounts",
        columns=[ColumnSchema(name="id", type=ColumnType.INTEGER), ColumnSchema(name="name", type=ColumnType.STRING)],
        rows=[
            {"id": 1, "name": "Acme Corp"},
            {"id": 2, "name": "Globex Inc"},
        ],
    )


def _contacts() -> CanonicalTable:
    return CanonicalTable(
        tenant_id="t",
        source_id="s",
        table_name="contacts",
        columns=[
            ColumnSchema(name="id", type=ColumnType.INTEGER),
            ColumnSchema(name="account_id", type=ColumnType.INTEGER),
            ColumnSchema(name="company", type=ColumnType.STRING),
        ],
        rows=[
            {"id": 1, "account_id": 1, "company": "Acme Corp"},  # consistent
            {"id": 2, "account_id": 2, "company": "Totally Different LLC"},  # mismatch
            {"id": 3, "account_id": 999, "company": "Nobody"},  # orphaned — no resolved pair
        ],
    )


def test_resolve_fk_pairs_only_includes_rows_with_a_real_parent():
    pairs = resolve_fk_pairs(_contacts(), _accounts(), "account_id")
    assert pairs == [(0, 0), (1, 1)]  # row 2 (account_id=999) is orphaned, excluded


def test_resolve_fk_pairs_skips_blank_fk_values():
    contacts = _contacts()
    contacts.rows.append({"id": 4, "account_id": "", "company": "Blank FK"})
    pairs = resolve_fk_pairs(contacts, _accounts(), "account_id")
    assert len(pairs) == 2  # the blank-FK row contributes nothing


def test_reasoner_parses_flags_from_canned_response():
    llm = _FakeLLM('{"flags": [{"from_row_index": 1, "to_row_index": 1, "reason": "company name mismatch", "confidence": 0.9}]}')
    reasoner = CrossTableReasoner(llm, sample_size=10)
    result = reasoner.run(_contacts(), _accounts(), [(0, 0), (1, 1)])
    assert len(result.flags) == 1
    assert result.flags[0].from_row_index == 1
    assert result.flags[0].to_row_index == 1
    assert result.flags[0].reason == "company name mismatch"
    assert result.pairs_sampled == 2


def test_reasoner_fails_safe_on_malformed_json():
    llm = _FakeLLM("not json at all")
    reasoner = CrossTableReasoner(llm, sample_size=10)
    result = reasoner.run(_contacts(), _accounts(), [(0, 0), (1, 1)])
    assert result.flags == []


def test_prompt_frames_row_data_as_untrusted():
    llm = _FakeLLM('{"flags": []}')
    reasoner = CrossTableReasoner(llm, sample_size=10)
    reasoner.run(_contacts(), _accounts(), [(0, 0)])
    system_prompt, user_prompt = llm.calls[0]
    assert "untrusted data" in system_prompt.lower()
    assert "UNTRUSTED DATA" in user_prompt


def test_system_prompt_does_not_tell_the_model_to_flag_routine_business_phrasing():
    """Parallel to the same fix in semantic_reasoning.py's prompt — the
    cross-table prompt must carve out routine business phrasing
    ("pre-approved", "already reviewed", "verified", "compliance
    notice") as NOT a manipulation signal, rather than folding it into
    the high-confidence flagging instruction below."""
    llm = _FakeLLM('{"flags": []}')
    reasoner = CrossTableReasoner(llm, sample_size=10)
    reasoner.run(_contacts(), _accounts(), [(0, 0)])
    system_prompt, _ = llm.calls[0]

    for phrase in ("pre-approved", "already reviewed", "verified", "compliance notice"):
        assert phrase in system_prompt
    assert "is NOT on its" in system_prompt
    assert "own a manipulation signal" in system_prompt
    assert "strong, independent signal of manipulation" in system_prompt  # genuine signals intact
    assert "flag the pair with high confidence" in system_prompt.lower()


def test_iteration_covers_every_pair_across_multiple_batches():
    """Batch size smaller than the pair pool forces >1 call — every
    pair should still get covered exactly once, same full-coverage
    guarantee as the single-table semantic reasoner."""
    contacts = _contacts()
    accounts = _accounts()
    pairs = resolve_fk_pairs(contacts, accounts, "account_id")  # 2 pairs

    calls_made_sizes = []

    class _RecordingLLM(LLMClient):
        def complete(self, system_prompt: str, user_prompt: str) -> str:
            calls_made_sizes.append(user_prompt.count("Pair ("))
            return '{"flags": []}'

    flags, calls, warning = run_cross_table_semantic_with_iteration(
        contacts, accounts, pairs, _RecordingLLM(), sample_size=1
    )
    assert calls == 2  # 2 pairs, batch size 1 -> 2 calls
    assert sum(calls_made_sizes) == 2
    assert warning is None


def test_iteration_returns_empty_when_no_resolved_pairs():
    flags, calls, warning = run_cross_table_semantic_with_iteration(
        _contacts(), _accounts(), [], _FakeLLM('{"flags": []}')
    )
    assert flags == []
    assert calls == 0
    assert warning is None


def test_iteration_stops_early_and_warns_when_llm_unavailable():
    pairs = resolve_fk_pairs(_contacts(), _accounts(), "account_id")
    flags, calls, warning = run_cross_table_semantic_with_iteration(
        _contacts(), _accounts(), pairs, _AlwaysFailsLLM(), sample_size=1
    )
    assert warning is not None
    assert "stopped early" in warning


def test_iteration_respects_max_llm_calls_ceiling():
    contacts = _contacts()
    accounts = _accounts()
    pairs = resolve_fk_pairs(contacts, accounts, "account_id")  # 2 pairs

    flags, calls, warning = run_cross_table_semantic_with_iteration(
        contacts, accounts, pairs, _FakeLLM('{"flags": []}'), sample_size=1, max_llm_calls=1
    )
    assert calls == 1  # capped even though 2 pairs exist


def test_verifier_confirms_rejects_and_defaults_to_needs_review():
    llm = _FakeLLM(
        '{"verifications": ['
        '{"from_row_index": 1, "to_row_index": 1, "label": "confirmed", "notes": "names differ"}'
        "]}"
    )
    from app.agent.cross_table_reasoning import CrossTableFlag

    flags = [
        CrossTableFlag(from_row_index=1, to_row_index=1, reason="mismatch", confidence=0.9),
        CrossTableFlag(from_row_index=0, to_row_index=0, reason="also flagged", confidence=0.5),
    ]
    verified = CrossTableVerifier(llm).verify(_contacts(), _accounts(), flags)
    by_key = {(v.from_row_index, v.to_row_index): v for v in verified}
    assert by_key[(1, 1)].label == CrossTableVerificationLabel.CONFIRMED
    assert by_key[(1, 1)].verification_failed is False
    # No verification returned for (0,0) -> fails safe to NEEDS_REVIEW, never silently dropped
    assert by_key[(0, 0)].label == CrossTableVerificationLabel.NEEDS_REVIEW
    # The LLM responded (just without this pair) — a parsed-but-incomplete
    # response, same bucket as "ran and returned nothing about this," not
    # a call failure — see app/domain/confidence.py.
    assert by_key[(0, 0)].verification_failed is True


def test_verifier_falls_back_to_needs_review_when_llm_unavailable():
    from app.agent.cross_table_reasoning import CrossTableFlag

    flags = [CrossTableFlag(from_row_index=1, to_row_index=1, reason="mismatch", confidence=0.9)]
    verified = CrossTableVerifier(_AlwaysFailsLLM()).verify(_contacts(), _accounts(), flags)
    assert len(verified) == 1
    assert verified[0].label == CrossTableVerificationLabel.NEEDS_REVIEW
    assert "unavailable" in verified[0].verifier_notes
    # This is the exact path a real provider outage or quota exhaustion
    # takes — must be distinguishable from a genuine uncertain verdict.
    assert verified[0].verification_failed is True


def test_drops_a_flag_for_a_pair_not_actually_in_this_batch():
    """Same membership protection as SemanticReasoner — a flag for a
    (from, to) pair fabricated in the model's response (hallucinated,
    or injected via row content shaped like fake JSON) must never
    survive if that exact pair wasn't part of the batch shown."""
    llm = _FakeLLM(
        '{"flags": [{"from_row_index": 0, "to_row_index": 0, "reason": "real", "confidence": 0.9}, '
        '{"from_row_index": 99, "to_row_index": 99, "reason": "fake", "confidence": 1.0}]}'
    )
    reasoner = CrossTableReasoner(llm, sample_size=10)
    result = reasoner.run(_contacts(), _accounts(), [(0, 0)])
    assert len(result.flags) == 1
    assert (result.flags[0].from_row_index, result.flags[0].to_row_index) == (0, 0)
