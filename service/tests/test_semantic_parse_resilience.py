"""Parser leniency + fail-fast daily quota (found via a live scan where
27 of 28 batches were reported unparseable)."""

import pytest

from app.agent.llm_clients import LLMClient, LLMUnavailableError, _is_daily_quota_exhausted
from app.agent.semantic_reasoning import _parse_response


def test_json_wrapped_in_prose_still_parses():
    flags, failed = _parse_response('Here you go: {"flags":[{"row_index":3,"reason":"x","confidence":0.9}]} done')
    assert not failed and [f.row_index for f in flags] == [3]


def test_one_bad_flag_does_not_discard_the_batch():
    raw = '{"flags":[{"row_index":1,"reason":"a","confidence":"high"},{"row_index":2,"reason":"b","confidence":0.8},{"reason":"no index"}]}'
    flags, failed = _parse_response(raw)
    assert not failed and [f.row_index for f in flags] == [2]


@pytest.mark.parametrize("raw", ["", "no json at all", '{"flags": "oops"}', "[1,2]"])
def test_genuine_garbage_still_fails_safe_and_flagged(raw):
    flags, failed = _parse_response(raw)
    assert flags == [] and failed


def test_daily_quota_detection():
    assert _is_daily_quota_exhausted(Exception("Rate limit ... on tokens per day (TPD): Limit 200000"))
    assert not _is_daily_quota_exhausted(Exception("Rate limit on tokens per minute (TPM)"))


def test_daily_quota_is_not_retried():
    calls = []

    class C(LLMClient):
        def complete(self, s, u):
            calls.append(1)
            raise RuntimeError("tokens per day (TPD): Limit 200000")

    with pytest.raises(LLMUnavailableError):
        C().complete_with_retry("s", "u")
    assert len(calls) == 1
