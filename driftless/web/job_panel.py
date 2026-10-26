"""Everything a job panel shows: the stages it goes through, and its result as a view."""

from dataclasses import dataclass
from typing import Any

from driftless.audit import AuditResult
from driftless.jobs import Job, JobMode, JobStatus
from driftless.progress import Stage
from driftless.sync import PullAnalysis
from driftless.web.views import analysis_view, check_view

STAGES: dict[JobMode, list[tuple[Stage, str]]] = {
    JobMode.CHECK: [(Stage.FETCH, "Fetching repo"), (Stage.START, "Starting app"), (Stage.RUN, "Running examples")],
    JobMode.FIX_EXAMPLES: [
        (Stage.FETCH, "Fetching repo"),
        (Stage.START, "Starting app"),
        (Stage.RUN, "Running & fixing examples"),
        (Stage.PUBLISH, "Opening PR"),
    ],
    JobMode.ANALYZE: [
        (Stage.FETCH, "Reading PR, docs & code"),
        (Stage.REVIEW, "Reviewing with the model"),
        (Stage.START, "Starting app"),
        (Stage.RUN, "Running examples"),
        (Stage.BASELINE, "Base commit (open PRs)"),
    ],
}
ACTIVE = frozenset({JobStatus.QUEUED, JobStatus.RUNNING})


@dataclass
class StageStep:
    label: str
    state: str  # done | current | pending


def stage_steps(job: Job) -> list[StageStep]:
    stages = STAGES.get(job.mode, [])
    if job.status == JobStatus.SUCCEEDED:
        return [StageStep(label, "done") for _, label in stages]
    keys = [stage for stage, _ in stages]
    current = keys.index(job.stage) if job.stage in keys else (-1 if job.status == JobStatus.QUEUED else 0)
    if job.status not in ACTIVE:
        current = max(current, 0)
    return [
        StageStep(label, "done" if i < current else "current" if i == current else "pending")
        for i, (_, label) in enumerate(stages)
    ]


def job_context(job: Job) -> dict[str, Any]:
    result = job.result
    return {
        "job": job,
        "active": job.status in ACTIVE,
        "steps": stage_steps(job),
        "check": check_view(result) if isinstance(result, AuditResult) else None,
        "analysis": analysis_view(result) if isinstance(result, PullAnalysis) else None,
        # Finished web jobs whose result was lost (server restart, or pushed out of memory).
        "result_gone": job.status == JobStatus.SUCCEEDED and job.mode in STAGES and result is None,
    }
