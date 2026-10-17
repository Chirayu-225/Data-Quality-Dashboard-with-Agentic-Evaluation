"""Stage and count events on scan jobs: the live stage rail and the
recordable timeline. Counts only, never row content."""

import io
import json
import time

from fastapi.testclient import TestClient

from app import jobs
from app.main import app

client = TestClient(app)

STAGES = ["checks", "instruction", "semantic", "verify", "score"]


def _csv(rows: list[list[str]], header=("email",)) -> bytes:
    return ("\n".join([",".join(header)] + [",".join(r) for r in rows]) + "\n").encode()


def _wait(job_id: str, query: str = "") -> dict:
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        body = client.get(f"/scans/jobs/{job_id}{query}").json()
        if body["status"] in ("done", "error", "cancelled"):
            return body
        time.sleep(0.03)
    raise AssertionError("job did not finish")


def _no_llm(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)


def _fake_llms(monkeypatch, flags_json='{"flags": []}'):
    monkeypatch.setenv("GROQ_API_KEY", "fake")
    monkeypatch.setenv("GEMINI_API_KEY", "fake")

    class _LLM:
        def complete_with_retry(self, system_prompt, user_prompt):
            return flags_json

    class _Ver:
        def complete_with_retry(self, system_prompt, user_prompt):
            return '{"verifications": []}'

    monkeypatch.setattr("app.agent.llm_clients.GroqClient", lambda: _LLM())
    monkeypatch.setattr("app.agent.llm_clients.GeminiClient", lambda: _Ver())
    monkeypatch.setattr("app.agent.generator._DEFAULT_PRODUCTION_SAMPLE_SIZE", 2)


def _submit(content: bytes, **data) -> str:
    r = client.post("/scans/jobs", data=data, files={"file": ("t.csv", io.BytesIO(content), "text/csv")})
    assert r.status_code == 200
    return r.json()["job_id"]


def _by_id(body):
    return {s["id"]: s for s in body["stages"]}


def test_stages_are_always_listed_in_pipeline_order_even_before_they_run():
    job = jobs.create_job()
    body = client.get(f"/scans/jobs/{job.job_id}").json()
    assert [s["id"] for s in body["stages"]] == STAGES
    assert all(s["status"] == "pending" for s in body["stages"])
    assert body["events"] is None


def test_deterministic_only_job_runs_checks_and_score_and_skips_ai_stages_with_reasons(monkeypatch):
    _no_llm(monkeypatch)
    jid = _submit(_csv([["a@x.com"], ["b@x.com"], ["not-an-email"]]))
    body = _wait(jid)
    st = _by_id(body)
    assert body["status"] == "done"
    assert st["checks"]["status"] == "done"
    assert st["checks"]["counts"]["checks_run"] >= 1
    assert st["checks"]["counts"]["checks_done"] == st["checks"]["counts"]["checks_total"]
    assert st["score"]["status"] == "done"
    assert st["score"]["counts"]["metrics_evaluated"] >= 1
    for sid in ("instruction", "semantic", "verify"):
        assert st[sid]["status"] == "skipped"
        assert st[sid]["reason"]


def test_ai_job_reports_semantic_row_counts_and_verify_stage(monkeypatch):
    _fake_llms(monkeypatch)
    jid = _submit(_csv([[f"u{i}@example.com"] for i in range(10)]), ai_review_consent="true")
    body = _wait(jid)
    st = _by_id(body)
    assert body["status"] == "done"
    assert st["instruction"]["status"] == "skipped"
    assert st["semantic"]["status"] == "done"
    assert st["semantic"]["counts"]["rows_considered"] == 10
    assert st["semantic"]["counts"]["rows_total"] == 10
    # No flags came back, so there is nothing for the verifier to check.
    assert st["verify"]["status"] == "skipped"
    assert st["verify"]["reason"] == "no flags to verify"


def test_verify_stage_counts_labels_when_flags_exist(monkeypatch):
    flags = json.dumps({"flags": [{"row_index": 0, "reason": "looks wrong", "confidence": 0.9}]})
    _fake_llms(monkeypatch, flags_json=flags)
    jid = _submit(_csv([[f"u{i}@example.com"] for i in range(4)]), ai_review_consent="true")
    st = _by_id(_wait(jid))
    assert st["semantic"]["counts"]["flags_raised"] > 0
    assert st["verify"]["status"] == "done"
    assert set(st["verify"]["counts"]) >= {"confirmed", "needs_review", "rejected"}


def test_events_are_opt_in_ordered_and_contain_only_integer_counts(monkeypatch):
    _fake_llms(monkeypatch)
    jid = _submit(_csv([[f"secret{i}@example.com"] for i in range(10)]), ai_review_consent="true")
    plain = _wait(jid)
    assert plain["events"] is None
    full = client.get(f"/scans/jobs/{jid}?events=true").json()
    events = full["events"]
    assert events and [e["t"] for e in events] == sorted(e["t"] for e in events)
    # Progress ticks exist for the semantic stage (several small batches).
    assert sum(1 for e in events if e["stage"] == "semantic" and e["state"] == "progress") >= 2
    # Counts are ints, and no row content leaks into the timeline.
    for e in events:
        for v in (e.get("counts") or {}).values():
            assert isinstance(v, int)
    assert "secret" not in json.dumps(events)
    # Stage order in the timeline follows the pipeline.
    first_seen = [s for s in STAGES if any(e["stage"] == s for e in events)]
    assert first_seen == [s for s in STAGES if s in first_seen]


def test_event_timeline_is_capped_and_keeps_state_changes():
    job = jobs.create_job()
    jobs.mark_stage(job.job_id, "semantic", "running", {"rows_considered": 0})
    for i in range(jobs.MAX_EVENTS * 2):
        jobs.mark_stage(job.job_id, "semantic", "progress", {"rows_considered": i})
    jobs.mark_stage(job.job_id, "semantic", "done", {"rows_considered": 999})
    j = jobs.get_job(job.job_id)
    assert len(j.events) <= jobs.MAX_EVENTS
    assert j.events[0]["state"] == "running"
    assert j.events[-1]["state"] == "done"
    assert j.stages["semantic"]["counts"]["rows_considered"] == 999


def test_reentering_a_finished_stage_restarts_it_with_fresh_counts():
    job = jobs.create_job()
    jobs.mark_stage(job.job_id, "checks", "running", {"checks_total": 5})
    jobs.mark_stage(job.job_id, "checks", "done", {"checks_run": 5})
    jobs.mark_stage(job.job_id, "checks", "running", {"checks_total": 3}, table="orders")
    st = jobs.get_job(job.job_id).stages["checks"]
    assert st["status"] == "running"
    assert st["counts"] == {"checks_total": 3}


def test_cancelled_job_ignores_late_stage_updates_and_callback_raises():
    from app.cancellation import JobCancelled

    job = jobs.create_job()
    cb = jobs.stage_callback_for(job.job_id)
    cb("checks", "running")
    jobs.cancel_job(job.job_id)
    try:
        cb("checks", "done")
    except JobCancelled:
        pass
    else:
        raise AssertionError("callback should raise after cancel")
    jobs.mark_stage(job.job_id, "score", "running")
    assert "score" not in jobs.get_job(job.job_id).stages


def test_schema_job_events_are_tagged_with_table_name(monkeypatch):
    _no_llm(monkeypatch)
    files = [
        ("files", ("a.csv", io.BytesIO(_csv([["x@x.com"]])), "text/csv")),
        ("files", ("b.csv", io.BytesIO(_csv([["y@x.com"]])), "text/csv")),
    ]
    r = client.post("/scans/schema/jobs", files=files)
    assert r.status_code == 200
    body = _wait(r.json()["job_id"], "?events=true")
    assert body["status"] == "done"
    tables = {e["table"] for e in body["events"] if e.get("table")}
    assert len(tables) == 2
