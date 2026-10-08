"""How jobs get executed.

``JobRunner`` is the seam between the API and the processing logic:

* ``ThreadPoolJobRunner`` (default) - runs jobs in a background thread pool
  inside the API process. Zero extra infrastructure.
* ``InlineJobRunner``               - runs the job synchronously. Used in tests
  so assertions don't need to poll or sleep.

Moving to a real task queue (Celery / RQ / Dramatiq) means adding one more
implementation whose ``submit`` enqueues ``job_id`` - nothing else changes,
because all state lives in the database, not in the runner.
"""

from __future__ import annotations

import logging
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Callable, Protocol

logger = logging.getLogger(__name__)

ProcessFn = Callable[[str], None]


class JobRunner(Protocol):
    def submit(self, job_id: str) -> None: ...
    def shutdown(self) -> None: ...


class InlineJobRunner:
    def __init__(self, process: ProcessFn) -> None:
        self._process = process

    def submit(self, job_id: str) -> None:
        self._process(job_id)

    def shutdown(self) -> None:
        pass


class ThreadPoolJobRunner:
    def __init__(self, process: ProcessFn, max_workers: int = 2) -> None:
        self._process = process
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="cert-worker")

    def submit(self, job_id: str) -> None:
        future = self._pool.submit(self._process, job_id)
        future.add_done_callback(lambda f, jid=job_id: self._log_crash(f, jid))

    @staticmethod
    def _log_crash(future: Future, job_id: str) -> None:
        exc = future.exception()
        if exc is not None:  # process_job already records failures; this is a last resort
            logger.error("Worker crashed while processing job %s", job_id, exc_info=exc)

    def shutdown(self) -> None:
        # Don't block shutdown on long jobs: unfinished certificates stay
        # 'pending' in the DB and are resumed on next start-up.
        self._pool.shutdown(wait=False, cancel_futures=True)
