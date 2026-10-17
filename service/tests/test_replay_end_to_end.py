"""System-level proof that a replayed LLM reply (zero latency, byte-identical
text) flows through the real pipeline and the async job layer without
breaking anything downstream: job ids, stage timelines, timestamps,
progress, scoring and the response shape.

The fake providers are real LLMClient subclasses that implement only
`complete`, so every call goes through LLMClient.complete_with_retry and
therefore through the replay cache. (The older fakes in test_main_api.py
override complete_with_retry directly and would bypass the cache.)
"""
import io
import time

import pytest
from fastapi.testclient import TestClient

from app.agent import replay_cache
from app.agent.llm_clients import LLMClient
from app.main import app

client = TestClient(app)

SLOW = 0.3  # seconds of simulated provider latency on a cache miss


class _SlowGenerator(LLMClient):
    _model = "fake-generator-1"
    calls = 0

    def complete(self, system_prompt: str, user_prompt: str) -> str:
        type(self).calls += 1
        time.sleep(SLOW)
        return '{"flags": [{"row_index": 1, "reason": "company name looks fake", "confidence": 0.9}]}'


class _SlowVerifier(LLMClient):
    _model = "fake-verifier-1"
    calls = 0

    def complete(self, system_prompt: str, user_prompt: str) -> str:
        type(self).calls += 1
        time.sleep(SLOW)
        return '{"verifications": [{"row_index": 1, "label": "confirmed", "notes": "supported by the row"}]}'


def _csv() -> bytes:
    rows = [
        "id,name,company,amount",
        "1,Ada Lovelace,Analytical Engines Ltd,100",
        "2,Grace Hopper,Zzxqv Holdings,200",
        "3,Alan Turing,Bletchley Systems,150",
        "4,Edsger Dijkstra,Mathematisch Centrum,175",
        "5,Barbara Liskov,Argus Labs,125",
    ]
    return ("\n".join(rows) + "\n").encode()


@pytest.fixture(autouse=True)
def _setup(monkeypatch, tmp_path):
    monkeypatch.setenv("GROQ_API_KEY", "fake-for-test")
    monkeypatch.setenv("GEMINI_API_KEY", "fake-for-test")
    monkeypatch.setenv("LLM_REPLAY_CACHE_DIR", str(tmp_path / "replay"))
    _SlowGenerator.calls = 0
    _SlowVerifier.calls = 0
    replay_cache.reset_stats_for_tests()
    monkeypatch.setattr("app.agent.llm_clients.GroqClient", lambda: _SlowGenerator())
    monkeypatch.setattr("app.agent.llm_clients.GeminiClient", lambda: _SlowVerifier())


def _run_job(extra: dict | None = None) -> dict:
    data = {"ai_review_consent": "true", "required_columns": "name,company"}
    data.update(extra or {})
    resp = client.post("/scans/jobs", data=data, files={"file": ("leads.csv", io.BytesIO(_csv()), "text/csv")})
    assert resp.status_code == 200
    job_id = resp.json()["job_id"]
    deadline = time.monotonic() + 30
    body: dict = {}
    while time.monotonic() < deadline:
        r = client.get(f"/scans/jobs/{job_id}", params={"events": "true"})
        assert r.status_code == 200
        body = r.json()
        if body["status"] in ("done", "error"):
            body["_job_id"] = job_id
            return body
        time.sleep(0.02)
    raise AssertionError(f"job never finished: {body}")


def _stable(result: dict) -> dict:
    """Everything in a scan result that must be identical across a replay."""
    return {
        "overall_score": result["overall_score"],
        "metrics": [(m["metric"], m["score"], m.get("evaluated", True)) for m in result["metric_scores"]],
        "flagged": [
            (f["row_index"], f.get("deterministic_reasons"), f.get("semantic_reason"), f.get("verification_label"))
            for f in result["flagged_rows"]
        ],
        "semantic_iterations": result["semantic_iterations"],
        "rows_considered": result["semantic_rows_considered"],
        "versions": result["versions"],
    }


def test_replayed_scan_is_identical_and_makes_no_second_provider_call():
    first = _run_job()
    assert first["status"] == "done", first.get("error")
    gen_calls, ver_calls = _SlowGenerator.calls, _SlowVerifier.calls
    assert gen_calls >= 1 and ver_calls >= 1  # the first run really used the providers
    assert replay_cache.stats()["hits"] == 0

    second = _run_job()
    assert second["status"] == "done", second.get("error")

    # No new provider traffic at all.
    assert _SlowGenerator.calls == gen_calls
    assert _SlowVerifier.calls == ver_calls
    assert replay_cache.stats()["hits"] == gen_calls + ver_calls

    # Same answer, field for field.
    assert _stable(second["result"]) == _stable(first["result"])
    # The semantic flag actually flowed through (guards against both runs being trivially empty).
    assert any(f.get("semantic_reason") for f in second["result"]["flagged_rows"])


def test_job_ids_and_clocks_stay_sane_when_latency_drops_to_zero():
    first = _run_job()
    second = _run_job()

    # Fresh, distinct, unguessable ids even though the payload is identical.
    assert first["_job_id"] != second["_job_id"]
    assert len(second["_job_id"]) >= 32

    # The replayed run really was faster, and no clock went negative.
    assert first["elapsed_seconds"] >= SLOW
    assert 0 <= second["elapsed_seconds"] < first["elapsed_seconds"]
    # The stage timeline of the replayed run is still well-formed.
    for run in (first, second):
        events = run["events"]
        assert events, "event timeline missing"
        times = [e["t"] for e in events]
        assert times == sorted(times), "event timestamps went backwards"
        assert all(t >= 0 for t in times)
        assert [st["id"] for st in run["stages"]] == ["checks", "instruction", "semantic", "verify", "score"]
        for st in run["stages"]:
            name = st["id"]
            assert st["status"] in ("done", "skipped"), (name, st)
            if st["started_at"] is not None and st["finished_at"] is not None:
                assert st["finished_at"] >= st["started_at"], (name, st)

    # Both runs report the same stage outcomes and counts, only the timings differ.
    def shape(run):
        return {s["id"]: (s["status"], s["counts"]) for s in run["stages"]}

    assert shape(second) == shape(first)


def test_progress_is_consistent_after_a_fully_replayed_run():
    _run_job()
    second = _run_job()
    p = second["progress"]
    assert p["total_rows"] >= 1
    assert p["rows_considered"] == p["total_rows"], p
    assert p["rows_unreadable"] == 0


def test_changing_the_prompt_misses_the_cache_and_calls_the_provider_again():
    _run_job()
    calls_before = _SlowGenerator.calls
    changed = _run_job({"custom_instruction": "flag leads whose company name looks fake"})
    assert changed["status"] == "done", changed.get("error")
    assert _SlowGenerator.calls > calls_before


def test_turning_the_cache_off_goes_back_to_live_calls(monkeypatch):
    _run_job()
    calls_before = _SlowGenerator.calls
    monkeypatch.delenv("LLM_REPLAY_CACHE_DIR")
    again = _run_job()
    assert again["status"] == "done"
    assert _SlowGenerator.calls > calls_before


def test_egress_ledger_reports_zero_egress_on_a_fully_replayed_scan():
    first = _run_job()["result"]["data_egress"]
    assert first["external_ai_used"] is True
    assert first["calls_live"] > 0 and first["calls_replayed"] == 0
    assert first["from_replay"] is False and first["rows_sent_to_provider"] > 0

    second = _run_job()["result"]["data_egress"]
    assert second["external_ai_used"] is False
    assert second["rows_sent_to_provider"] == 0 and second["providers_used"] == []
    assert second["calls_live"] == 0 and second["calls_replayed"] == first["calls_live"]
    assert second["from_replay"] is True and second["partly_from_replay"] is False
    assert "replayed" in second["note"]
