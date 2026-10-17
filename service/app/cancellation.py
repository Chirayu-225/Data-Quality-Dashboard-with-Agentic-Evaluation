"""Cooperative cancellation shared by the job store and the LLM client.

A running scan executes on a worker thread and may sit in a semantic
batch, a verification chunk, the cross-table pass, a rule compile, or a
rate-limit sleep. Rather than thread a flag through every signature,
the job runner installs a per-thread check here, and every provider
call (LLMClient.complete_with_retry) and every pacing sleep consults
it. That is the one choke point all phases share, so cancel reaches
each of them within at most one in-flight call.

JobCancelled derives from BaseException on purpose: the scan code has
broad `except Exception` handlers that fail safe on provider errors,
and a cancel must unwind THROUGH them instead of being swallowed and
recorded as a coverage gap."""

from __future__ import annotations

import threading
from typing import Callable


class JobCancelled(BaseException):
    """The job was cancelled (explicitly, or auto-cancelled as abandoned)."""


_LOCAL = threading.local()


def set_check(check: Callable[[], bool] | None) -> None:
    _LOCAL.check = check


def current_check() -> Callable[[], bool] | None:
    return getattr(_LOCAL, "check", None)


def raise_if_cancelled() -> None:
    check = current_check()
    if check is not None and check():
        raise JobCancelled()
