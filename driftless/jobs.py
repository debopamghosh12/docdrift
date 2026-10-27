"""Registry of background jobs (last N kept), one active job per PR and lane.

Webhooks and the web app queue the same jobs. Pre-merge and post-merge runs share a lane: a
newer push (or the merge) supersedes a running pre-merge check, which stops at its next
checkpoint without commenting. `/driftless apply` has its own lane, so it never cancels a check.
Memory commands (`/driftless ignore` ...) get a lane per comment: two different commands never
dedupe, a redelivered one does. The web app's dry-run analyses get a lane per PR, and docs health
audits one per repo and ref (`pr` is 0 for those).

A running job reports its stage (`stage`, `detail`); its full result (an audit report, an
analysis) stays in memory for the web app to render.

With a `JobLog` (the SQLite memory) every state change is saved, so the history survives a
restart; jobs a previous process left queued or running are marked failed on startup.
"""

import logging
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from enum import Enum
from typing import Any, NamedTuple, Protocol

from pydantic import BaseModel, PrivateAttr

from driftless.pipeline import OwnPullRequestError
from driftless.pr_writer import PublishResult
from driftless.premerge import SupersededError
from driftless.progress import Stage, StageProgress

logger = logging.getLogger("driftless.jobs")


class JobMode(str, Enum):
    PRE_MERGE = "pre-merge"
    POST_MERGE = "post-merge"
    APPLY = "apply"
    MEMORY = "memory"  # `/driftless ignore|unignore|style|unstyle|memory`
    ANALYZE = "analyze"  # web: dry run of a PR, publishes nothing
    CHECK = "check"  # web: docs health audit of a ref (`driftless check`)
    FIX_EXAMPLES = "fix-examples"  # web: audit, fix broken examples, open a PR (`driftless check --fix`)


REPO_MODES = frozenset({JobMode.CHECK, JobMode.FIX_EXAMPLES})  # not about one PR: `pr` is 0


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
    requested_by: str | None = None  # apply / memory: who commented
    command: str | None = None  # memory: the command, "/driftless ignore CHANGELOG.md"
    comment_id: int | None = None  # memory: the comment it came from
    ref: str | None = None  # check / fix-examples: the branch, tag or commit audited
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
    stage: Stage | None = None  # while running: what it is doing
    detail: str | None = None
    summary: str | None = None  # check: "4/5 examples passing"; analyze: "2 doc files to change"
    _stop: threading.Event = PrivateAttr(default_factory=threading.Event)
    _result: Any = PrivateAttr(default=None)

    @property
    def result(self) -> Any:
        """The run's full result (AuditResult, PullAnalysis or PublishResult), while in memory."""
        return self._result

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

    def remember(self, repo: str, number: int, user: str, command: str) -> PublishResult:
        """Run a memory command and reply on the PR (write access only)."""
        ...

    def analyze(self, repo: str, number: int, progress: StageProgress) -> "Outcome":
        """Dry run of a PR; the result has `published = None` and a `summary`."""
        ...

    def check(self, repo: str, ref: str, fix: bool, progress: StageProgress) -> "Outcome":
        """Docs health audit; with `fix`, `published` is the examples PR (or None)."""
        ...


class Outcome(Protocol):
    """A web job's result: what it published (if anything) and a one-line summary."""

    @property
    def published(self) -> PublishResult | None: ...

    @property
    def summary(self) -> str: ...


class JobLog(Protocol):
    """Where jobs are persisted (implemented by memory.Memory)."""

    def save_job(self, job_id: str, repo: str, pr: int, status: str, queued_at: float, data: dict[str, Any]) -> None: ...

    def recent_jobs(self, limit: int = 20, repo: str | None = None) -> list[dict[str, Any]]: ...

    def job(self, job_id: str) -> dict[str, Any] | None: ...

    def interrupt_unfinished(self) -> int: ...


class JobRefused(Exception):
    """An `admit` check declined to start a new job (the message says why, for the user)."""


Admit = Callable[[list["Job"]], None]  # given the active jobs; raises JobRefused to decline


class Enqueued(NamedTuple):
    job: Job
    created: bool
    superseded: Job | None = None


class JobStore:
    def __init__(self, syncer: Syncer, keep: int = 20, log: JobLog | None = None) -> None:
        self._syncer = syncer
        self._keep = keep
        self._jobs: deque[Job] = deque(maxlen=keep)
        self._active: dict[tuple[str, int, str], Job] = {}
        self._lock = threading.Lock()
        self._log = log
        if log is not None:
            interrupted = log.interrupt_unfinished()
            if interrupted:
                logger.warning("marked %d job(s) from a previous run as interrupted", interrupted)

    def enqueue(
        self,
        repo: str,
        pr: int,
        mode: JobMode = JobMode.POST_MERGE,
        head_sha: str | None = None,
        requested_by: str | None = None,
        command: str | None = None,
        comment_id: int | None = None,
        ref: str | None = None,
        admit: Admit | None = None,
    ) -> Enqueued:
        """A redelivery (same mode and head) gets the active job back; a newer push or the merge
        supersedes an active pre-merge check. `admit` runs (under the lock) only when a new job
        would be created, so limits never refuse showing a run that is already going."""
        key = _key(repo, pr, mode, comment_id, ref)
        with self._lock:
            active = self._active.get(key)
            if active and not _supersedes(active, mode, head_sha):
                return Enqueued(active, False)
            if admit is not None:
                admit(list(self._active.values()))
            job = Job(
                id=uuid.uuid4().hex[:12], repo=repo, pr=pr, mode=mode, head_sha=head_sha,
                requested_by=requested_by, command=command, comment_id=comment_id, ref=ref, queued_at=time.time(),
            )
            if active:
                active.supersede(job)
                logger.info("job %s superseded by %s for %s#%d", active.id, job.id, repo, pr)
                self._save(active)
            self._active[key] = job
            self._jobs.append(job)
            self._save(job)
        logger.info("job %s queued (%s) for %s#%d", job.id, mode.value, repo, pr)
        return Enqueued(job, True, active)

    def run(self, job: Job) -> None:
        job.started_at = time.time()
        try:
            if job.superseded:
                raise SupersededError(f"superseded by {job.superseded_by} before it started")
            job.status = JobStatus.RUNNING
            self._save(job)
            logger.info("job %s started (%s): %s#%d", job.id, job.mode.value, job.repo, job.pr)
            outcome = self._dispatch(job)
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
            job._result, job.status = outcome, JobStatus.SUCCEEDED
            published = outcome if isinstance(outcome, PublishResult) else outcome.published
            if not isinstance(outcome, PublishResult):
                job.summary = outcome.summary
            if published is not None:
                job.action, job.docs_pr_url = published.action.value, published.url
                job.check_state = published.check_state
            logger.info("job %s finished: %s", job.id, published.url if published else job.summary)
        finally:
            job.finished_at = time.time()
            job.duration_s = round(job.finished_at - job.started_at, 2)
            job.stage = job.detail = None
            key = _key(job.repo, job.pr, job.mode, job.comment_id, job.ref)
            with self._lock:
                if self._active.get(key) is job:  # a superseding job owns the slot now
                    del self._active[key]
                self._save(job)

    def recent(self, repo: str | None = None, limit: int | None = None) -> list[Job]:
        """Newest first (optionally one repo's); jobs still in memory keep their results."""
        limit = limit or self._keep
        if self._log is None:
            with self._lock:
                jobs = [j for j in reversed(self._jobs) if repo is None or j.repo.lower() == repo.lower()]
            return jobs[:limit]
        with self._lock:
            live = {j.id: j for j in self._jobs}
        rows = self._log.recent_jobs(limit, repo=repo)
        return [live.get(data["id"]) or Job.model_validate(data) for data in rows]

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            live = next((j for j in self._jobs if j.id == job_id), None)
        if live is not None or self._log is None:
            return live
        data = self._log.job(job_id)
        return Job.model_validate(data) if data else None

    def _progress(self, job: Job, stage: Stage, detail: str) -> None:
        job.stage, job.detail = stage, detail
        self._save(job)

    def _save(self, job: Job) -> None:
        if self._log is None:
            return
        try:
            self._log.save_job(job.id, job.repo, job.pr, job.status.value, job.queued_at, job.model_dump(mode="json"))
        except Exception:  # losing a history row must never fail the job
            logger.exception("could not save job %s", job.id)

    def _dispatch(self, job: Job) -> PublishResult | Outcome:
        def progress(stage: Stage, detail: str) -> None:
            self._progress(job, stage, detail)

        if job.mode == JobMode.ANALYZE:
            return self._syncer.analyze(job.repo, job.pr, progress)
        if job.mode in REPO_MODES:
            return self._syncer.check(job.repo, job.ref or "", job.mode == JobMode.FIX_EXAMPLES, progress)
        if job.mode == JobMode.PRE_MERGE:
            return self._syncer.pre_merge(job.repo, job.pr, job.head_sha or "", lambda: job.superseded)
        if job.mode == JobMode.APPLY:
            return self._syncer.apply(job.repo, job.pr, job.requested_by or "")
        if job.mode == JobMode.MEMORY:
            return self._syncer.remember(job.repo, job.pr, job.requested_by or "", job.command or "")
        return self._syncer.post_merge(job.repo, job.pr)


def _key(
    repo: str, pr: int, mode: JobMode, comment_id: int | None = None, ref: str | None = None
) -> tuple[str, int, str]:
    if mode == JobMode.MEMORY:
        return repo.lower(), pr, f"memory:{comment_id}"
    if mode in REPO_MODES:
        return repo.lower(), 0, f"{mode.value}:{ref}"
    if mode in (JobMode.APPLY, JobMode.ANALYZE):
        return repo.lower(), pr, mode.value
    return repo.lower(), pr, "sync"


def _supersedes(active: Job, mode: JobMode, head_sha: str | None) -> bool:
    """Only a pre-merge check can be superseded: by the merge, or by a check of a newer push."""
    if active.mode != JobMode.PRE_MERGE:
        return False
    return mode == JobMode.POST_MERGE or head_sha != active.head_sha
