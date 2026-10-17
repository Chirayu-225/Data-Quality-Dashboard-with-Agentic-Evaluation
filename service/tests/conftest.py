"""Test-suite defaults.

Local value review (the no-LLM semantic tier) is ON by default in the
product, which changes what an AI-off scan returns (accuracy is
evaluated, semantic flags can appear). The existing suite was written
against the AI-off-means-deterministic-only contract, so it runs with
LOCAL_REVIEW=0; tests that exercise local review turn it on explicitly
(see tests/test_local_value_review.py)."""
import pytest


@pytest.fixture(autouse=True)
def _legacy_ai_off_contract(monkeypatch):
    monkeypatch.setenv("LOCAL_REVIEW", "0")
