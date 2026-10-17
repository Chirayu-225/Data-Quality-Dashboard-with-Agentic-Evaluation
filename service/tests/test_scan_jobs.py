"""
Tests for the async scan-job endpoints (/scans/jobs, /scans/jobs/{id}) —
the submit/poll alternative to the synchronous /scans, built specifically
so a long semantic scan doesn't have to survive one held-open HTTP
request (see app/jobs.py's module docstring)."""

import io
import time

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def _csv_bytes(header: list[str], rows: list[list[str]]) -> bytes:
    lines = [",".join(header)] + [",".join(r) for r in rows]
    return ("\n".join(lines) + "\n").encode("utf-8")


def _poll_until_terminal(job_id: str, timeout_seconds: float = 5.0) -> dict:
    """Polls GET /scans/jobs/{job_id} until status is "done" or "error",
    or `timeout_seconds` elapses — the background thread runs on its
    own schedule, so a test checking its outcome has to poll rather
    than assume any particular timing."""
    deadline = time.monotonic() + timeout_seconds
    last_body: dict = {}
    while time.monotonic() < deadline:
        resp = client.get(f"/scans/jobs/{job_id}")
        assert resp.status_code == 200
        last_body = resp.json()
        if last_body["status"] in ("done", "error"):
            return last_body
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} never reached a terminal status in time: {last_body}")


def test_polling_an_unknown_job_id_returns_404():
    resp = client.get("/scans/jobs/does-not-exist")
    assert resp.status_code == 404


def test_submit_returns_a_job_id_immediately(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    csv_content = _csv_bytes(["amount"], [["100"], ["200"]])
    resp = client.post(
        "/scans/jobs",
        files={"file": ("amounts.csv", io.BytesIO(csv_content), "text/csv")},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "job_id" in body and body["job_id"]


def test_a_deterministic_only_job_reaches_done_with_the_same_result_shape_as_scans(monkeypatch):
    """No LLM configured -> no progress updates at all (nothing to
    report progress ON), but the job still runs the full deterministic
    pipeline and lands on "done" with a real ScanResponse-shaped
    result — the exact same contract /scans itself returns, just
    reached by polling instead of blocking."""
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    csv_content = _csv_bytes(
        ["id", "amount"],
        [["0", "100"], ["1", ""], ["2", "150"]],
    )
    resp = client.post(
        "/scans/jobs",
        data={"required_columns": "amount"},
        files={"file": ("amounts.csv", io.BytesIO(csv_content), "text/csv")},
    )
    assert resp.status_code == 200
    job_id = resp.json()["job_id"]

    final = _poll_until_terminal(job_id)
    assert final["status"] == "done"
    assert final["error"] is None
    assert final["result"] is not None
    assert final["result"]["llm_used"] is False
    assert 1 in [r["row_index"] for r in final["result"]["flagged_rows"]]


def test_job_progress_advances_as_semantic_batches_complete(monkeypatch):
    """The core of the async-job feature: a caller polling mid-scan
    sees progress move, not silence. Uses a fake LLM/batch size small
    enough to force multiple iterations of the semantic loop, so there
    are multiple progress updates to observe, not just a single jump
    from 0 to total."""
    monkeypatch.setenv("GROQ_API_KEY", "fake-for-test")
    monkeypatch.setenv("GEMINI_API_KEY", "fake-for-test")

    class _SlowFakeLLM:
        def complete_with_retry(self, system_prompt: str, user_prompt: str) -> str:
            time.sleep(0.05)  # gives the poller a real window to observe "running"
            return '{"flags": []}'

    class _FakeVerifier:
        def complete_with_retry(self, system_prompt: str, user_prompt: str) -> str:
            return '{"verifications": []}'

    monkeypatch.setattr("app.agent.llm_clients.GroqClient", lambda: _SlowFakeLLM())
    monkeypatch.setattr("app.agent.llm_clients.GeminiClient", lambda: _FakeVerifier())
    # Small sample size forces several batches/iterations over a
    # modest row count, instead of covering everything in one call.
    monkeypatch.setattr("app.agent.generator._DEFAULT_PRODUCTION_SAMPLE_SIZE", 2)

    rows = [[f"row{i}@example.com"] for i in range(10)]
    csv_content = _csv_bytes(["email"], rows)
    resp = client.post(
        "/scans/jobs",
        data={"ai_review_consent": "true"},
        files={"file": ("leads.csv", io.BytesIO(csv_content), "text/csv")},
    )
    assert resp.status_code == 200
    job_id = resp.json()["job_id"]

    # Poll at least once while it should still be in flight, to prove
    # progress is observable DURING the scan, not only after.
    saw_nonzero_progress_before_done = False
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        body = client.get(f"/scans/jobs/{job_id}").json()
        if body["status"] == "done":
            break
        if body["progress"]["rows_considered"] > 0:
            saw_nonzero_progress_before_done = True
        time.sleep(0.02)
    else:
        raise AssertionError("job never finished in time")

    final = _poll_until_terminal(job_id)
    assert final["status"] == "done"
    assert final["result"]["llm_used"] is True
    assert final["progress"]["total_rows"] == 10
    assert final["progress"]["rows_considered"] == 10
    assert saw_nonzero_progress_before_done


def test_a_scan_that_raises_lands_the_job_on_error_not_a_silent_hang(monkeypatch):
    """A background job that blows up must be visible to a poller as
    status "error" with a message — never stuck on "running" forever
    and never silently swallowed."""
    monkeypatch.setenv("GROQ_API_KEY", "fake-for-test")
    monkeypatch.setenv("GEMINI_API_KEY", "fake-for-test")

    class _ExplodingLLM:
        def complete_with_retry(self, system_prompt: str, user_prompt: str) -> str:
            raise RuntimeError("simulated provider meltdown")

    monkeypatch.setattr("app.agent.llm_clients.GroqClient", lambda: _ExplodingLLM())
    monkeypatch.setattr("app.agent.llm_clients.GeminiClient", lambda: _ExplodingLLM())

    csv_content = _csv_bytes(["email"], [["a@example.com"]])
    resp = client.post(
        "/scans/jobs",
        data={"ai_review_consent": "true"},
        files={"file": ("leads.csv", io.BytesIO(csv_content), "text/csv")},
    )
    assert resp.status_code == 200
    job_id = resp.json()["job_id"]

    final = _poll_until_terminal(job_id)
    assert final["status"] == "error"
    assert "simulated provider meltdown" in final["error"]
    assert final["result"] is None


# --- Hardening: bounded queue, TTL eviction, unguessable ids, schema jobs ---


def _reset_jobs():
    from app import jobs

    with jobs._LOCK:
        jobs._JOBS.clear()


def test_job_ids_are_long_random_urlsafe_tokens_not_sequential(monkeypatch):
    from app import jobs

    monkeypatch.setenv("SCAN_JOB_MAX_PENDING", "100")
    _reset_jobs()
    ids = {jobs.create_job().job_id for _ in range(20)}
    assert len(ids) == 20
    # token_urlsafe(32) -> 43 chars of 256-bit CSPRNG output.
    assert all(len(i) >= 43 for i in ids)
    assert not any(i.isdigit() for i in ids)
    _reset_jobs()


def test_queue_cap_returns_429_instead_of_accepting_unbounded_work(monkeypatch):
    import threading

    monkeypatch.setenv("SCAN_JOB_MAX_PENDING", "1")
    monkeypatch.setenv("SCAN_JOB_WORKERS", "1")
    monkeypatch.setenv("GROQ_API_KEY", "fake-for-test")
    monkeypatch.setenv("GEMINI_API_KEY", "fake-for-test")
    _reset_jobs()
    release = threading.Event()

    class _BlockingLLM:
        def complete_with_retry(self, system_prompt: str, user_prompt: str) -> str:
            release.wait(timeout=5)
            return '{"flags": []}'

    class _FakeVerifier:
        def complete_with_retry(self, system_prompt: str, user_prompt: str) -> str:
            return '{"verifications": []}'

    monkeypatch.setattr("app.agent.llm_clients.GroqClient", lambda: _BlockingLLM())
    monkeypatch.setattr("app.agent.llm_clients.GeminiClient", lambda: _FakeVerifier())

    csv_content = _csv_bytes(["email"], [["a@example.com"]])
    first = client.post(
        "/scans/jobs",
        data={"ai_review_consent": "true"},
        files={"file": ("leads.csv", io.BytesIO(csv_content), "text/csv")},
    )
    assert first.status_code == 200
    second = client.post(
        "/scans/jobs",
        data={"ai_review_consent": "true"},
        files={"file": ("leads.csv", io.BytesIO(csv_content), "text/csv")},
    )
    assert second.status_code == 429
    assert second.headers.get("retry-after")

    release.set()
    _poll_until_terminal(first.json()["job_id"])
    # Capacity frees up once the first job finishes.
    third = client.post(
        "/scans/jobs",
        data={"ai_review_consent": "true"},
        files={"file": ("leads.csv", io.BytesIO(csv_content), "text/csv")},
    )
    assert third.status_code == 200
    _poll_until_terminal(third.json()["job_id"])
    _reset_jobs()


def test_finished_jobs_are_evicted_after_the_ttl_but_running_ones_are_not(monkeypatch):
    from app import jobs

    _reset_jobs()
    monkeypatch.setenv("SCAN_JOB_TTL_SECONDS", "10")
    finished = jobs.create_job()
    jobs.mark_done(finished.job_id, {"ok": True})
    running = jobs.create_job()
    jobs.mark_running(running.job_id)

    with jobs._LOCK:
        jobs._JOBS[finished.job_id].finished_at -= 11  # age past the TTL
        jobs._JOBS[running.job_id].created_at -= 10_000  # old, but unfinished

    assert jobs.get_job(finished.job_id) is None
    assert jobs.get_job(running.job_id) is not None
    _reset_jobs()


def test_schema_job_runs_all_tables_and_reports_aggregate_progress(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "fake-for-test")
    monkeypatch.setenv("GEMINI_API_KEY", "fake-for-test")
    _reset_jobs()

    class _FakeLLM:
        def complete_with_retry(self, system_prompt: str, user_prompt: str) -> str:
            return '{"flags": []}'

    class _FakeVerifier:
        def complete_with_retry(self, system_prompt: str, user_prompt: str) -> str:
            return '{"verifications": []}'

    monkeypatch.setattr("app.agent.llm_clients.GroqClient", lambda: _FakeLLM())
    monkeypatch.setattr("app.agent.llm_clients.GeminiClient", lambda: _FakeVerifier())

    accounts = _csv_bytes(["id", "name"], [["1", "Acme"], ["2", "Globex"]])
    leads = _csv_bytes(["id", "account_id"], [["10", "1"], ["11", "2"], ["12", "1"]])
    resp = client.post(
        "/scans/schema/jobs",
        data={"ai_review_consent": "true"},
        files=[
            ("files", ("accounts.csv", io.BytesIO(accounts), "text/csv")),
            ("files", ("leads.csv", io.BytesIO(leads), "text/csv")),
        ],
    )
    assert resp.status_code == 200
    final = _poll_until_terminal(resp.json()["job_id"])
    assert final["status"] == "done", final.get("error")
    assert final["result"]["total_tables"] == 2
    assert final["result"]["total_rows"] == 5
    assert final["progress"]["total_rows"] == 5
    assert final["progress"]["rows_considered"] == 5
    _reset_jobs()


# --- Cancel, expiry (410) ---


def test_cancel_a_running_job_stops_it_and_it_never_flips_back(monkeypatch):
    import threading

    monkeypatch.setenv("GROQ_API_KEY", "fake-for-test")
    monkeypatch.setenv("GEMINI_API_KEY", "fake-for-test")
    monkeypatch.setattr("app.agent.generator._DEFAULT_PRODUCTION_SAMPLE_SIZE", 2)
    _reset_jobs()
    in_call = threading.Event()
    release = threading.Event()
    calls = []

    class _GatedLLM:
        def complete_with_retry(self, system_prompt: str, user_prompt: str) -> str:
            calls.append(1)
            in_call.set()
            release.wait(timeout=5)
            return '{"flags": []}'

    class _V:
        def complete_with_retry(self, s, u):
            return '{"verifications": []}'

    monkeypatch.setattr("app.agent.llm_clients.GroqClient", lambda: _GatedLLM())
    monkeypatch.setattr("app.agent.llm_clients.GeminiClient", lambda: _V())

    rows = [[f"r{i}@example.com"] for i in range(20)]
    resp = client.post(
        "/scans/jobs",
        data={"ai_review_consent": "true"},
        files={"file": ("l.csv", io.BytesIO(_csv_bytes(["email"], rows)), "text/csv")},
    )
    job_id = resp.json()["job_id"]
    assert in_call.wait(timeout=5)

    cancelled = client.delete(f"/scans/jobs/{job_id}")
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
    release.set()
    time.sleep(0.5)  # worker unwinds at the next batch boundary
    final = client.get(f"/scans/jobs/{job_id}").json()
    assert final["status"] == "cancelled" and final["result"] is None
    assert len(calls) <= 2  # 10 batches were possible; it stopped early
    _reset_jobs()


def test_cancel_is_idempotent_and_leaves_finished_jobs_alone(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    resp = client.post(
        "/scans/jobs", files={"file": ("a.csv", io.BytesIO(_csv_bytes(["a"], [["1"]])), "text/csv")}
    )
    job_id = resp.json()["job_id"]
    assert _poll_until_terminal(job_id)["status"] == "done"
    again = client.delete(f"/scans/jobs/{job_id}")
    assert again.status_code == 200 and again.json()["status"] == "done"
    assert client.delete("/scans/jobs/nope").status_code == 404


def test_expired_job_returns_410_not_a_generic_404(monkeypatch):
    from app import jobs

    _reset_jobs()
    monkeypatch.setenv("SCAN_JOB_TTL_SECONDS", "10")
    j = jobs.create_job()
    jobs.mark_done(j.job_id, {"ok": True})
    with jobs._LOCK:
        jobs._JOBS[j.job_id].finished_at -= 11
    resp = client.get(f"/scans/jobs/{j.job_id}")
    assert resp.status_code == 410
    assert "expired" in resp.json()["detail"].lower()
    assert client.get("/scans/jobs/never-existed").status_code == 404
    _reset_jobs()


# --- Heartbeat auto-cancel, cancel reaching every phase ---


def test_unpolled_job_is_auto_cancelled_but_a_polled_one_survives(monkeypatch):
    from app import jobs

    _reset_jobs()
    monkeypatch.setenv("SCAN_JOB_ABANDON_SECONDS", "60")
    abandoned = jobs.create_job()
    alive = jobs.create_job()
    jobs.mark_running(abandoned.job_id)
    jobs.mark_running(alive.job_id)
    with jobs._LOCK:
        jobs._JOBS[abandoned.job_id].last_polled_at -= 61
        jobs._JOBS[alive.job_id].last_polled_at -= 61
    jobs.get_job(alive.job_id, touch=True)  # a client poll = heartbeat
    # touch happens before the sweep, so `alive` is spared
    assert jobs.get_job(alive.job_id).status.value == "running"
    gone = jobs.get_job(abandoned.job_id)
    assert gone.status.value == "cancelled" and "Auto-cancelled" in gone.error
    _reset_jobs()


def test_internal_reads_do_not_keep_an_abandoned_job_alive(monkeypatch):
    from app import jobs

    _reset_jobs()
    monkeypatch.setenv("SCAN_JOB_ABANDON_SECONDS", "60")
    j = jobs.create_job()
    jobs.mark_running(j.job_id)
    with jobs._LOCK:
        jobs._JOBS[j.job_id].last_polled_at -= 61
    assert jobs.is_cancelled(j.job_id)  # the worker's own check notices
    _reset_jobs()


def test_abandon_can_be_disabled(monkeypatch):
    from app import jobs

    _reset_jobs()
    monkeypatch.setenv("SCAN_JOB_ABANDON_SECONDS", "0")
    j = jobs.create_job()
    with jobs._LOCK:
        jobs._JOBS[j.job_id].last_polled_at -= 100_000
    assert jobs.get_job(j.job_id).status.value == "queued"
    _reset_jobs()


def test_cancel_during_verification_stops_remaining_verifier_calls(monkeypatch):
    """Cancel must reach phases beyond the semantic loop: here the
    generator flags many rows, the verifier is mid-way through its
    chunks, and cancelling stops it before the remaining chunks."""
    import threading

    monkeypatch.setenv("GROQ_API_KEY", "fake-for-test")
    monkeypatch.setenv("GEMINI_API_KEY", "fake-for-test")
    _reset_jobs()
    in_verify = threading.Event()
    release = threading.Event()
    verifier_calls = []

    class _FlagAll:
        def complete_with_retry(self, s, u):
            from app.cancellation import raise_if_cancelled

            raise_if_cancelled()
            import json as _j

            return _j.dumps({"flags": [{"row_index": i, "reason": "x", "confidence": 0.9} for i in range(20)]})

    class _GatedVerifier:
        def complete_with_retry(self, s, u):
            from app.cancellation import raise_if_cancelled

            raise_if_cancelled()
            verifier_calls.append(1)
            in_verify.set()
            release.wait(timeout=5)
            return '{"verifications": []}'

    monkeypatch.setattr("app.agent.llm_clients.GroqClient", lambda: _FlagAll())
    monkeypatch.setattr("app.agent.llm_clients.GeminiClient", lambda: _GatedVerifier())

    rows = [[f"r{i}@example.com"] for i in range(30)]
    resp = client.post(
        "/scans/jobs",
        data={"ai_review_consent": "true"},
        files={"file": ("l.csv", io.BytesIO(_csv_bytes(["email"], rows)), "text/csv")},
    )
    job_id = resp.json()["job_id"]
    assert in_verify.wait(timeout=5)
    assert client.delete(f"/scans/jobs/{job_id}").json()["status"] == "cancelled"
    release.set()
    time.sleep(0.5)
    assert len(verifier_calls) == 1  # remaining chunks never called
    assert client.get(f"/scans/jobs/{job_id}").json()["status"] == "cancelled"
    _reset_jobs()


def test_progress_reports_unreadable_rows(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "fake-for-test")
    monkeypatch.setenv("GEMINI_API_KEY", "fake-for-test")
    monkeypatch.setattr("app.agent.generator._DEFAULT_PRODUCTION_SAMPLE_SIZE", 5)
    _reset_jobs()

    class _Garbage:
        def complete_with_retry(self, s, u):
            return "not json at all"

    class _V:
        def complete_with_retry(self, s, u):
            return '{"verifications": []}'

    monkeypatch.setattr("app.agent.llm_clients.GroqClient", lambda: _Garbage())
    monkeypatch.setattr("app.agent.llm_clients.GeminiClient", lambda: _V())
    rows = [[f"r{i}@example.com"] for i in range(10)]
    resp = client.post(
        "/scans/jobs",
        data={"ai_review_consent": "true"},
        files={"file": ("l.csv", io.BytesIO(_csv_bytes(["email"], rows)), "text/csv")},
    )
    final = _poll_until_terminal(resp.json()["job_id"])
    assert final["status"] == "done"
    assert final["progress"]["rows_considered"] == 0
    assert final["progress"]["rows_unreadable"] == 10
    _reset_jobs()


def test_status_reports_elapsed_seconds_running_then_frozen_when_done(monkeypatch):
    from app import jobs

    _reset_jobs()
    j = jobs.create_job()
    jobs.mark_running(j.job_id)
    with jobs._LOCK:
        jobs._JOBS[j.job_id].created_at -= 12
    running = client.get(f"/scans/jobs/{j.job_id}").json()
    assert 11.5 <= running["elapsed_seconds"] <= 14
    jobs.mark_done(j.job_id, {"ok": True})
    first = client.get(f"/scans/jobs/{j.job_id}").json()["elapsed_seconds"]
    time.sleep(0.3)
    assert client.get(f"/scans/jobs/{j.job_id}").json()["elapsed_seconds"] == first  # frozen
    _reset_jobs()
