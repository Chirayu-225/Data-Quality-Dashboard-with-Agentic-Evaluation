"""
In-memory async job store for long-running scans.

Why this exists: a 5,000-row semantic scan at ~45 rows/call (the
production batch size — see generator.py's
`_DEFAULT_PRODUCTION_SAMPLE_SIZE`) is 100+ paced LLM calls under a
tokens-per-minute budget. That can take many minutes, inside what
/scans and /scans/schema run as one synchronous HTTP request. A
browser, the Next.js proxy in front of this service, and most hosting
platforms will all time out long before a scan like that finishes —
this was flagged directly in a product review as "the first thing a
real user with a real file will hit," and the row cap added in
DETECTION_VERSION 1.10.0 (app/config.py's max_semantic_reasoning_rows)
only raises the table size at which this becomes a problem, it
doesn't solve it.

The fix is the usual shape for a slow operation behind a web API:
submit, get an id back immediately, poll for status/progress, fetch
the result once it's done. main.py's /scans/jobs (submit) and
/scans/jobs/{job_id} (poll) are the two endpoints built on top of this
module. Progress comes from the SAME coverage numbers this project
already tracks for the "semantic coverage" warning
(semantic_rows_considered vs. total rows) — see generator.py's new
`progress_callback` parameter on GeneratorAgent.run/
_run_semantic_with_iteration, called after every batch; nothing new
had to be invented to get a real, incrementally-updating progress bar
out of this, just a hook to report numbers that were already being
computed.

Deliberately in-memory, not persisted or queued through a real task
broker — matches this service's current phase (main.py's own module
docstring: DB/auth/persistence are all frozen for now, deliberately).

Safety properties (the store is exposed with no auth, so these matter):

- BOUNDED CONCURRENCY. Jobs run on a fixed-size ThreadPoolExecutor
  (config.scan_job_workers, default 2), not a thread per submission.
- BOUNDED QUEUE. At most config.scan_job_max_pending jobs may be queued
  or running at once; past that, `create_job` raises `JobQueueFull` and
  main.py answers 429 instead of accepting unbounded work.
- TTL EVICTION. A finished job (done/error) is dropped
  config.scan_job_ttl_seconds (default 1 hour) after it finished —
  evicted lazily on every create/get, so no sweeper thread is needed.
  Its result holds unmasked flagged rows; it should not live forever.
- UNGUESSABLE IDS. Job ids are `secrets.token_urlsafe(32)` (256 bits of
  CSPRNG output), never sequential. With no auth, the id is the ONLY
  access control on a result, so it must not be enumerable.

Known remaining limits (deployment notes, also in docs/DEPLOYMENT.md):

- SINGLE PROCESS ONLY. `_JOBS` lives in one process's memory. Run the
  service with ONE worker process (e.g. `uvicorn app.main:app`, NOT
  `--workers 4` / gunicorn -w N / multiple replicas): a job submitted to
  one process 404s when polled on another. Scaling out needs a shared
  store (Redis, a DB table) — a code change, not a config flag.
- A job is lost if the process restarts; the poller gets a 404.
- Progress is only as fine-grained as one semantic batch.
"""

from __future__ import annotations

import secrets
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

from app import cancellation
from app.cancellation import JobCancelled
from app.config import (
    scan_job_abandon_seconds,
    scan_job_max_pending,
    scan_job_ttl_seconds,
    scan_job_workers,
)


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    ERROR = "error"
    CANCELLED = "cancelled"


@dataclass
class JobProgress:
    """`rows_considered` mirrors ScanResult.semantic_rows_considered —
    rows a genuinely successful, parsed semantic batch has covered so
    far, not merely "attempted." `total_rows` is the table's real row
    count (see generator.py's docstring on why that's deliberately not
    shrunk for quarantined rows). Both start at 0: a job with no LLM
    configured (deterministic-only) never gets a progress update at
    all and simply goes straight from `running` to `done`, finishing
    fast — see main.py's job-runner thread."""

    rows_considered: int = 0
    total_rows: int = 0
    # Rows sampled whose model reply could not be parsed - attempted but
    # NOT reliably reviewed. Lets the UI say "N rows unreadable" instead
    # of looking stuck.
    rows_unreadable: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "rows_considered": self.rows_considered,
            "total_rows": self.total_rows,
            "rows_unreadable": self.rows_unreadable,
        }


STAGE_ORDER = ("checks", "instruction", "semantic", "verify", "score")
# Cap on the replayable event timeline. A scan of many batches or many
# tables must not grow a job without bound; once full, the OLDEST
# progress events are dropped first-in, but state-change events
# (running/done/skipped) are always kept.
MAX_EVENTS = 400


@dataclass
class Job:
    job_id: str
    status: JobStatus = JobStatus.QUEUED
    progress: JobProgress = field(default_factory=JobProgress)
    # The finished response, already shaped as a JSON-serializable dict
    # (main.py builds this from ScanResponse.model_dump() before
    # storing it) — this module has no opinion on what a scan response
    # looks like, deliberately, so it stays reusable for both /scans
    # and /scans/schema's job variants.
    result: dict[str, Any] | None = None
    error: str | None = None
    created_at: float = field(default_factory=time.monotonic)
    updated_at: float = field(default_factory=time.monotonic)
    finished_at: float | None = None
    # Heartbeat: updated ONLY by client polls (GET), not by the worker.
    last_polled_at: float = field(default_factory=time.monotonic)
    # Pipeline stage state, keyed by stage id (STAGE_ORDER). Counts only,
    # never row content: this is shown live and recorded for replay.
    stages: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Timeline of stage events, offsets in seconds since job creation.
    events: list[dict[str, Any]] = field(default_factory=list)


_JOBS: dict[str, Job] = {}
_LOCK = threading.Lock()
# Tombstones (id -> evicted-at) so a poller can tell "expired, run it
# again" (410) from "never existed / server restarted" (404). Ids only,
# no data; bounded and itself time-limited.
_EXPIRED: dict[str, float] = {}
_EXPIRED_MAX = 5000
_EXPIRED_KEEP_SECONDS = 24 * 3600
_EXECUTOR: ThreadPoolExecutor | None = None


_TERMINAL = (JobStatus.DONE, JobStatus.ERROR, JobStatus.CANCELLED)


class JobQueueFull(RuntimeError):
    """Too many queued/running jobs — the caller should answer 429."""


def _executor() -> ThreadPoolExecutor:
    global _EXECUTOR
    with _LOCK:
        if _EXECUTOR is None:
            _EXECUTOR = ThreadPoolExecutor(
                max_workers=scan_job_workers(), thread_name_prefix="scan-job"
            )
        return _EXECUTOR


def _cancel_abandoned_locked(now: float) -> None:
    """Auto-cancel queued/running jobs nobody has polled recently."""
    limit = scan_job_abandon_seconds()
    if limit <= 0:
        return
    for job in _JOBS.values():
        if job.status in (JobStatus.QUEUED, JobStatus.RUNNING) and now - job.last_polled_at > limit:
            job.status = JobStatus.CANCELLED
            job.result = None
            job.error = (
                f"Auto-cancelled: nothing checked on this scan for {limit} s, so it was "
                f"assumed abandoned (closed tab) and stopped to save provider quota."
            )
            job.updated_at = job.finished_at = now


def _evict_expired_locked(now: float) -> None:
    _cancel_abandoned_locked(now)
    ttl = scan_job_ttl_seconds()
    expired = [
        job_id
        for job_id, job in _JOBS.items()
        if job.finished_at is not None and now - job.finished_at > ttl
    ]
    for job_id in expired:
        del _JOBS[job_id]
        _EXPIRED[job_id] = now
    for job_id in [j for j, t in _EXPIRED.items() if now - t > _EXPIRED_KEEP_SECONDS]:
        del _EXPIRED[job_id]
    while len(_EXPIRED) > _EXPIRED_MAX:
        _EXPIRED.pop(next(iter(_EXPIRED)))


def create_job() -> Job:
    """Registers a new queued job, or raises JobQueueFull when
    queued+running jobs already sit at the configured cap."""
    now = time.monotonic()
    with _LOCK:
        _evict_expired_locked(now)
        pending = sum(
            1 for j in _JOBS.values() if j.status in (JobStatus.QUEUED, JobStatus.RUNNING)
        )
        if pending >= scan_job_max_pending():
            raise JobQueueFull(
                f"{pending} scan job(s) already queued or running (limit "
                f"{scan_job_max_pending()}); retry shortly."
            )
        job = Job(job_id=secrets.token_urlsafe(32))
        _JOBS[job.job_id] = job
        return job


def was_expired(job_id: str) -> bool:
    with _LOCK:
        _evict_expired_locked(time.monotonic())
        return job_id in _EXPIRED


def cancel_job(job_id: str) -> Job | None:
    """Idempotent. Queued jobs never start; a running job is marked
    cancelled immediately (what pollers see) and its worker unwinds at
    the next batch boundary via JobCancelled, so at most ONE more
    provider call can still be in flight. Finished jobs are unchanged."""
    with _LOCK:
        _evict_expired_locked(time.monotonic())
        job = _JOBS.get(job_id)
        if job is None:
            return None
        if job.status not in _TERMINAL:
            job.status = JobStatus.CANCELLED
            job.result = None
            job.updated_at = job.finished_at = time.monotonic()
        return job


def get_job(job_id: str, touch: bool = False) -> Job | None:
    """`touch=True` marks this as a client poll (the heartbeat). Only the
    polling endpoint passes it - internal reads by the worker must not
    keep an abandoned job alive."""
    with _LOCK:
        now = time.monotonic()
        job = _JOBS.get(job_id)
        if touch and job is not None and job.status not in _TERMINAL:
            job.last_polled_at = now
        _evict_expired_locked(now)
        return _JOBS.get(job_id)


def is_cancelled(job_id: str) -> bool:
    """True once cancelled explicitly or auto-cancelled as abandoned.
    This is what the worker thread's cancellation check calls."""
    with _LOCK:
        _evict_expired_locked(time.monotonic())
        job = _JOBS.get(job_id)
        return job is None or job.status == JobStatus.CANCELLED


def _update(job_id: str, **fields: Any) -> None:
    with _LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            return  # evicted mid-run (TTL) — nothing to update
        if job.status == JobStatus.CANCELLED:
            return  # a cancelled job never flips back to done/error/running
        for key, value in fields.items():
            setattr(job, key, value)
        job.updated_at = time.monotonic()
        if job.status in _TERMINAL and job.finished_at is None:
            job.finished_at = job.updated_at


def mark_running(job_id: str) -> None:
    _update(job_id, status=JobStatus.RUNNING)


def mark_progress(job_id: str, rows_considered: int, total_rows: int, rows_unreadable: int = 0) -> None:
    _update(
        job_id,
        progress=JobProgress(
            rows_considered=rows_considered, total_rows=total_rows, rows_unreadable=rows_unreadable
        ),
    )


def mark_stage(
    job_id: str,
    stage: str,
    state: str,
    counts: dict[str, int] | None = None,
    reason: str | None = None,
    table: str | None = None,
) -> None:
    """Records a stage transition or a progress tick.

    `state` is one of running | progress | done | skipped. "progress"
    updates counts on a running stage without changing its status. A
    stage that is re-entered (the next table of a schema scan) restarts
    as running with fresh counts; the event timeline keeps every entry,
    each tagged with its table. Counts are integers only by contract."""
    with _LOCK:
        job = _JOBS.get(job_id)
        if job is None or job.status == JobStatus.CANCELLED:
            return
        t = round(time.monotonic() - job.created_at, 2)
        st = job.stages.get(stage)
        if st is None or (state == "running" and st.get("status") in ("done", "skipped")):
            st = {"status": "pending", "started_at": None, "finished_at": None, "counts": {}, "reason": None}
            job.stages[stage] = st
        if state == "running":
            st["status"] = "running"
            st["started_at"] = t
            st["finished_at"] = None
        elif state in ("done", "skipped"):
            st["status"] = state
            if st["started_at"] is None:
                st["started_at"] = t
            st["finished_at"] = t
        if counts:
            st["counts"] = {**st["counts"], **{k: int(v) for k, v in counts.items()}}
        if reason is not None:
            st["reason"] = reason
        event: dict[str, Any] = {"t": t, "stage": stage, "state": state}
        if counts:
            event["counts"] = {k: int(v) for k, v in counts.items()}
        if reason is not None:
            event["reason"] = reason
        if table is not None:
            event["table"] = table
        job.events.append(event)
        if len(job.events) > MAX_EVENTS:
            for i, e in enumerate(job.events):
                if e["state"] == "progress":
                    del job.events[i]
                    break
            else:
                del job.events[0]
        job.updated_at = time.monotonic()


def stage_callback_for(job_id: str, table: str | None = None) -> Callable[..., None]:
    """The `(stage, state, counts=None, reason=None)` callback the
    pipeline calls at stage boundaries. Like the progress callback it
    raises JobCancelled once the job is cancelled, so cancel also
    reaches the deterministic phase between checks."""

    def _callback(
        stage: str, state: str, counts: dict[str, int] | None = None, reason: str | None = None
    ) -> None:
        if is_cancelled(job_id):
            raise JobCancelled()
        mark_stage(job_id, stage, state, counts, reason, table)

    return _callback


def mark_done(job_id: str, result: dict[str, Any]) -> None:
    _update(job_id, status=JobStatus.DONE, result=result)


def mark_error(job_id: str, error: str) -> None:
    _update(job_id, status=JobStatus.ERROR, error=error)


def progress_callback_for(job_id: str) -> Callable[[int, int], None]:
    """Builds the `(rows_considered, total_rows)` callback the
    generator calls after every semantic batch. A closure so
    generator.py stays unaware this module exists."""

    def _callback(rows_considered: int, total_rows: int, rows_unreadable: int = 0) -> None:
        if is_cancelled(job_id):
            raise JobCancelled()
        mark_progress(job_id, rows_considered, total_rows, rows_unreadable)

    return _callback


def submit(job_id: str, target: Callable[[], dict[str, Any]]) -> None:
    """Queues `target` (a zero-arg callable that runs the scan and
    returns a JSON-serializable dict) on the bounded worker pool. A
    real thread pool, not asyncio: GeneratorAgent.run is a long
    synchronous call with real sleeps for rate-limit pacing, and
    awaiting it on the API's event loop would block every other
    request. ANY exception lands on the job as status "error"."""

    def _runner() -> None:
        if is_cancelled(job_id):
            return  # cancelled while queued: never start
        mark_running(job_id)
        # Every provider call and pacing sleep on THIS thread consults
        # this check (app/cancellation.py), so cancel reaches the
        # semantic loop, verification, cross-table and rule compilation.
        cancellation.set_check(lambda: is_cancelled(job_id))
        try:
            result = target()
        except JobCancelled:
            return
        except Exception as exc:  # noqa: BLE001 - must never leave a job stuck "running"
            mark_error(job_id, f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}")
            return
        finally:
            cancellation.set_check(None)
        mark_done(job_id, result)

    _executor().submit(_runner)


# Back-compat alias for the first version's name.
run_in_background = submit
