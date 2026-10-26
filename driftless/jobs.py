"""In-memory registry of webhook-triggered jobs (last N kept), one active job per PR and lane.

Pre-merge and post-merge runs share a lane: a newer push (or the merge) supersedes a running
pre-merge check, which stops at its next checkpoint without commenting. `/driftless apply`
has its own lane, so it never cancels a check.
"""

import logging
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from enum import Enum
from typing import NamedTuple, Protocol

from pydantic import BaseModel, PrivateAttr

from driftless.pipeline import OwnPullRequestError
from driftless.pr_writer import PublishResult
from driftless.premerge import SupersededError

logger = logging.getLogger("driftless.jobs")


class JobMode(str, Enum):
    PRE_MERGE = "pre-merge"
    POST_MERGE = "post-merge"
    APPLY = "apply"


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    SKIPPED = "skipped"
    SUPERSEDED = "superseded"
    FAILED = "failed"


class Job(BaseModel):
    id: str
    repo: str
    pr: int
    mode: JobMode = JobMode.POST_MERGE
    head_sha: str | None = None  # pre-merge: the pushed commit being checked
    requested_by: str | None = None  # apply: who commented `/driftless apply`
    status: JobStatus = JobStatus.QUEUED
    queued_at: float
    started_at: float | None = None
    finished_at: float | None = None
    duration_s: float | None = None
    action: str | None = None  # opened / updated / commented / committed / declined
    docs_pr_url: str | None = None  # docs PR, comment or commit URL
    check_state: str | None = None  # pre-merge commit status
    superseded_by: str | None = None
    error: str | None = None
    _stop: threading.Event = PrivateAttr(default_factory=threading.Event)

    @property
    def superseded(self) -> bool:
        return self._stop.is_set()

    def supersede(self, by: "Job") -> None:
        self.superseded_by = by.id
        self._stop.set()


class Syncer(Protocol):
    """What the server runs for each mode (implemented by sync.GitHubSyncer)."""

    def post_merge(self, repo: str, number: int) -> PublishResult: ...

    def pre_merge(self, repo: str, number: int, head_sha: str, should_stop: Callable[[], bool]) -> PublishResult:
        """Raises SupersededError once `should_stop()` is true (or the head moved past `head_sha`)."""
        ...

    def apply(self, repo: str, number: int, user: str) -> PublishResult: ...


class Enqueued(NamedTuple):
    job: Job
    created: bool
    superseded: Job | None = None


class JobStore:
    def __init__(self, syncer: Syncer, keep: int = 20) -> None:
        self._syncer = syncer
        self._jobs: deque[Job] = deque(maxlen=keep)
        self._active: dict[tuple[str, int, str], Job] = {}
        self._lock = threading.Lock()

    def enqueue(
        self,
        repo: str,
        pr: int,
        mode: JobMode = JobMode.POST_MERGE,
        head_sha: str | None = None,
        requested_by: str | None = None,
    ) -> Enqueued:
        """A redelivery (same mode and head) gets the active job back; a newer push or the merge
        supersedes an active pre-merge check."""
        key = _key(repo, pr, mode)
        with self._lock:
            active = self._active.get(key)
            if active and not _supersedes(active, mode, head_sha):
                return Enqueued(active, False)
            job = Job(
                id=uuid.uuid4().hex[:12], repo=repo, pr=pr, mode=mode,
                head_sha=head_sha, requested_by=requested_by, queued_at=time.time(),
            )
            if active:
                active.supersede(job)
                logger.info("job %s superseded by %s for %s#%d", active.id, job.id, repo, pr)
            self._active[key] = job
            self._jobs.append(job)
        logger.info("job %s queued (%s) for %s#%d", job.id, mode.value, repo, pr)
        return Enqueued(job, True, active)

    def run(self, job: Job) -> None:
        job.started_at = time.time()
        try:
            if job.superseded:
                raise SupersededError(f"superseded by {job.superseded_by} before it started")
            job.status = JobStatus.RUNNING
            logger.info("job %s started (%s): %s#%d", job.id, job.mode.value, job.repo, job.pr)
            published = self._dispatch(job)
        except SupersededError as exc:
            job.status, job.error = JobStatus.SUPERSEDED, str(exc)
            logger.info("job %s superseded: %s", job.id, exc)
        except OwnPullRequestError as exc:
            job.status, job.error = JobStatus.SKIPPED, str(exc)
            logger.info("job %s skipped: %s", job.id, exc)
        except Exception as exc:  # a background job must never crash the server
            job.status, job.error = JobStatus.FAILED, f"{type(exc).__name__}: {exc}"
            logger.exception("job %s failed", job.id)
        else:
            job.status, job.action, job.docs_pr_url = JobStatus.SUCCEEDED, published.action.value, published.url
            job.check_state = published.check_state
            logger.info("job %s finished: %s %s", job.id, published.action.value, published.url)
        finally:
            job.finished_at = time.time()
            job.duration_s = round(job.finished_at - job.started_at, 2)
            key = _key(job.repo, job.pr, job.mode)
            with self._lock:
                if self._active.get(key) is job:  # a superseding job owns the slot now
                    del self._active[key]

    def recent(self) -> list[Job]:
        with self._lock:
            return list(reversed(self._jobs))

    def _dispatch(self, job: Job) -> PublishResult:
        if job.mode == JobMode.PRE_MERGE:
            return self._syncer.pre_merge(job.repo, job.pr, job.head_sha or "", lambda: job.superseded)
        if job.mode == JobMode.APPLY:
            return self._syncer.apply(job.repo, job.pr, job.requested_by or "")
        return self._syncer.post_merge(job.repo, job.pr)


def _key(repo: str, pr: int, mode: JobMode) -> tuple[str, int, str]:
    return repo.lower(), pr, "apply" if mode == JobMode.APPLY else "sync"


def _supersedes(active: Job, mode: JobMode, head_sha: str | None) -> bool:
    """Only a pre-merge check can be superseded: by the merge, or by a check of a newer push."""
    if active.mode != JobMode.PRE_MERGE:
        return False
    return mode == JobMode.POST_MERGE or head_sha != active.head_sha
