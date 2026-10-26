"""In-memory registry of webhook-triggered sync jobs (last N kept), one active job per source PR."""

import logging
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from enum import Enum

from pydantic import BaseModel

from docdrift.pipeline import OwnPullRequestError
from docdrift.pr_writer import PublishResult

logger = logging.getLogger("docdrift.jobs")

SyncFn = Callable[[str, int], PublishResult]


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    SKIPPED = "skipped"
    FAILED = "failed"


class Job(BaseModel):
    id: str
    repo: str
    pr: int
    status: JobStatus = JobStatus.QUEUED
    queued_at: float
    started_at: float | None = None
    finished_at: float | None = None
    duration_s: float | None = None
    action: str | None = None  # opened / updated / commented
    docs_pr_url: str | None = None
    error: str | None = None


class JobStore:
    def __init__(self, sync: SyncFn, keep: int = 20) -> None:
        self._sync = sync
        self._jobs: deque[Job] = deque(maxlen=keep)
        self._active: dict[tuple[str, int], Job] = {}
        self._lock = threading.Lock()

    def enqueue(self, repo: str, pr: int) -> tuple[Job, bool]:
        """Return (job, created). An already queued/running job for the same PR is returned as-is."""
        key = (repo.lower(), pr)
        with self._lock:
            if key in self._active:
                return self._active[key], False
            job = Job(id=uuid.uuid4().hex[:12], repo=repo, pr=pr, queued_at=time.time())
            self._active[key] = job
            self._jobs.append(job)
        logger.info("job %s queued for %s#%d", job.id, repo, pr)
        return job, True

    def run(self, job: Job) -> None:
        job.status = JobStatus.RUNNING
        job.started_at = time.time()
        logger.info("job %s started: %s#%d", job.id, job.repo, job.pr)
        try:
            published = self._sync(job.repo, job.pr)
        except OwnPullRequestError as exc:
            job.status, job.error = JobStatus.SKIPPED, str(exc)
            logger.info("job %s skipped: %s", job.id, exc)
        except Exception as exc:  # a background job must never crash the server
            job.status, job.error = JobStatus.FAILED, f"{type(exc).__name__}: {exc}"
            logger.exception("job %s failed", job.id)
        else:
            job.status, job.action, job.docs_pr_url = JobStatus.SUCCEEDED, published.action.value, published.url
            logger.info("job %s finished: %s %s", job.id, published.action.value, published.url)
        finally:
            job.finished_at = time.time()
            job.duration_s = round(job.finished_at - job.started_at, 2)
            with self._lock:
                self._active.pop((job.repo.lower(), job.pr), None)

    def recent(self) -> list[Job]:
        with self._lock:
            return list(reversed(self._jobs))
