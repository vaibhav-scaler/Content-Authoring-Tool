"""Keeps several authors working at once from stepping on each other. By code, no model call.

Two protections, both held in this process (the app runs as one web process; with more than one, each process keeps its own count):

  1. One AI job per script at a time. A second request that would start the same kind of work on the same script (a double click, two tabs, two authors) is turned away at once
     with a clear sentence, instead of writing the same card twice and paying twice. Different scripts never wait for each other.
  2. A limit on how many model calls run at the same time (LLM_MAX_CONCURRENT, default 6). A burst queues for a free place instead of all hitting the provider's rate limit,
     and a request that waits longer than LLM_QUEUE_WAIT_SECONDS (default 90) is told the AI is busy and to try again shortly.
"""
import os
import re
import threading
import time
from contextlib import contextmanager
from typing import Dict, Optional

STALE_AFTER_SECONDS = 15 * 60          # a job older than this is treated as lost (a crash or a closed tab), so a script is never stuck "busy"


class JobGuard:
    def __init__(self, stale_after: float = STALE_AFTER_SECONDS):
        self._lock = threading.Lock()
        self._running: Dict[str, float] = {}
        self._stale_after = stale_after

    def try_start(self, key: str) -> bool:
        now = time.time()
        with self._lock:
            started = self._running.get(key)
            if started is not None and now - started < self._stale_after:
                return False
            self._running[key] = now
            return True

    def finish(self, key: str) -> None:
        with self._lock:
            self._running.pop(key, None)

    def running(self, key: str) -> bool:
        with self._lock:
            started = self._running.get(key)
            return started is not None and time.time() - started < self._stale_after


jobs = JobGuard()

# What counts as the same kind of work on a script. Outline work blocks outline work; card writing, chat, quizzes and review all change the cards, so they block each other;
# a diagram is regenerated one at a time per diagram.
_ROUTES = [
    (re.compile(r"^/api/scripts/(\d+)/outline(?:/action|/card-assist)?$"), "outline"),
    (re.compile(r"^/api/scripts/(\d+)/(?:generate-section|draft-all|chat-assist|review|review/apply|cards/\d+/quiz/regenerate)$"), "cards"),
    (re.compile(r"^/api/scripts/(\d+)/setup/generate$"), "setup"),
    (re.compile(r"^/api/scripts/(\d+)/generate-assignments$"), "assignments"),
    (re.compile(r"^/api/scripts/(\d+)/visuals/([^/]+)/regenerate$"), "visual"),
]
_WHAT = {"outline": "the outline", "cards": "its cards", "setup": "its setup list", "assignments": "its assignments", "visual": "this diagram"}


def job_key(method: str, path: str) -> Optional[str]:
    """The key of the job a request would start, or None for a request that is not AI work (a read, a manual edit)."""
    if method.upper() != "POST":
        return None
    for pattern, kind in _ROUTES:
        m = pattern.match(path)
        if m:
            return f"{kind}:{m.group(1)}" + (f":{m.group(2)}" if kind == "visual" else "")
    return None


def busy_message(key: str) -> str:
    return (f"The AI is already working on {_WHAT.get(key.split(':')[0], 'this')} of this script. Wait for it to finish, then try again. "
            "(This stops the same card being written twice, and paid for twice.)")


class LLMGate:
    """At most `limit` model calls at once; the rest wait their turn."""

    def __init__(self):
        self._sem: Optional[threading.BoundedSemaphore] = None
        self._limit = 0
        self._make = threading.Lock()

    @staticmethod
    def _env_int(name: str, default: int) -> int:
        try:
            return max(1, int(os.getenv(name, default)))
        except ValueError:
            return default

    def _semaphore(self) -> threading.BoundedSemaphore:
        limit = self._env_int("LLM_MAX_CONCURRENT", 6)
        with self._make:
            if self._sem is None or limit != self._limit:
                self._sem, self._limit = threading.BoundedSemaphore(limit), limit
            return self._sem

    @contextmanager
    def slot(self):
        """Wait for a free place. Raises TimeoutError when none frees up in time (the caller turns that into a readable message)."""
        sem = self._semaphore()
        wait = float(self._env_int("LLM_QUEUE_WAIT_SECONDS", 90))
        if not sem.acquire(timeout=wait):
            raise TimeoutError(f"waited {int(wait)} seconds for a free place")
        try:
            yield
        finally:
            sem.release()


llm_gate = LLMGate()
