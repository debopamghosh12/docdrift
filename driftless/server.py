"""FastAPI webhook server for a watched repo.

- `pull_request` opened / synchronize / reopened / ready_for_review: pre-merge check of the head
  commit (one PR comment, edited in place, and a commit status).
- `pull_request` closed + merged: post-merge sync (docs PR, or an in-sync comment).
- `issue_comment` "/driftless apply" on an open PR: commit the verified doc fixes to its branch.
- `issue_comment` "/driftless ignore|unignore|style|unstyle|memory ...": change or show what
  Driftless remembers for the repo.
- `pull_request` closed on one of Driftless's own docs PRs: remember its changes as accepted
  (merged) or rejected (closed without merging).

The same process serves the web app (`driftless.web`): every page and action for the repos on
ALLOWED_REPOS, with long runs queued on the same job store.
"""

import hashlib
import hmac
import json
import logging
from typing import Any

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from driftless.config import Settings, get_settings
from driftless.events import EventKind, EventPlan, route_event
from driftless.jobs import Enqueued, Job, JobMode, JobStore, Syncer
from driftless.memory import Memory
from driftless.web import install_web
from driftless.web.access import redact
from driftless.web.github_browser import RepoBrowser

logger = logging.getLogger("driftless.server")


def verify_signature(secret: str, body: bytes, signature: str | None) -> bool:
    """Check GitHub's `X-Hub-Signature-256: sha256=<hex>` header. No secret configured = reject."""
    if not secret or not signature or not signature.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature.removeprefix("sha256="))


def create_app(
    settings: Settings | None = None,
    syncer: Syncer | None = None,
    memory: Memory | None = None,
    browser: RepoBrowser | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    memory = memory or Memory(settings.database_url)
    if syncer is None:
        from driftless.sync import GitHubSyncer

        syncer = GitHubSyncer(settings, memory=memory)
    if not settings.webhook_secret:
        logger.warning("WEBHOOK_SECRET is not set; every webhook delivery will be rejected")

    store = JobStore(syncer, log=memory)
    app = FastAPI(title="Driftless")
    app.state.jobs = store
    app.state.memory = memory
    app.state.web = install_web(app, settings, store, memory, browser)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/runs")
    def runs() -> list[Job]:
        return [job.model_copy(update={"error": redact(job.error, settings) or None}) for job in store.recent()]

    @app.post("/webhook")
    async def webhook(
        request: Request,
        background: BackgroundTasks,
        x_github_event: str = Header(""),
        x_hub_signature_256: str | None = Header(None),
    ) -> JSONResponse:
        body = await request.body()
        if not verify_signature(settings.webhook_secret, body, x_hub_signature_256):
            raise HTTPException(status_code=401, detail="invalid signature")
        try:
            payload: dict[str, Any] = json.loads(body)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail="body is not JSON") from exc

        if x_github_event == "ping":
            return JSONResponse({"status": "ok", "zen": payload.get("zen")})
        return _dispatch(store, memory, background, x_github_event, route_event(x_github_event, payload))

    return app


def _dispatch(
    store: JobStore, memory: Memory, background: BackgroundTasks, event: str, plan: EventPlan
) -> JSONResponse:
    repo, number = plan.repo, plan.number
    if plan.kind == EventKind.DOCS_PR_CLOSED:
        return _docs_pr_closed(memory, repo, number, plan.merged)
    if plan.kind == EventKind.POST_MERGE:
        return _queue(store, background, store.enqueue(repo, number, JobMode.POST_MERGE))
    if plan.kind == EventKind.PRE_MERGE:
        return _queue(store, background, store.enqueue(repo, number, JobMode.PRE_MERGE, head_sha=plan.head_sha))
    if plan.kind == EventKind.APPLY:
        return _queue(store, background, store.enqueue(repo, number, JobMode.APPLY, requested_by=plan.user))
    if plan.kind == EventKind.MEMORY and plan.command is not None:
        enqueued = store.enqueue(
            repo, number, JobMode.MEMORY, requested_by=plan.user, command=plan.command.text, comment_id=plan.comment_id
        )
        return _queue(store, background, enqueued)
    return _ignored(plan.reason or f"event {event!r}")  # audits are scheduled in Actions, not by webhooks


def _docs_pr_closed(memory: Memory, repo: str, number: int, merged: bool) -> JSONResponse:
    """Feedback: a merged docs PR's changes were accepted, a closed one's rejected."""
    changes = memory.resolve_docs_pr(repo, number, merged)
    if changes is None:
        return _ignored("Driftless's own docs PR, not opened by this server")
    outcome = "accepted" if merged else "rejected"
    logger.info("remembered %d change(s) of docs PR %s#%d as %s", len(changes), repo, number, outcome)
    return JSONResponse({"status": "remembered", "outcome": outcome, "changes": len(changes)})


def _queue(store: JobStore, background: BackgroundTasks, enqueued: Enqueued) -> JSONResponse:
    job = enqueued.job.model_dump(mode="json")
    if not enqueued.created:
        return JSONResponse({"status": "already running", "job": job})
    background.add_task(store.run, enqueued.job)
    body: dict[str, Any] = {"status": "queued", "job": job}
    if enqueued.superseded:
        body["superseded"] = enqueued.superseded.id
    return JSONResponse(body, status_code=202)


def _ignored(reason: str) -> JSONResponse:
    logger.info("ignored webhook: %s", reason)
    return JSONResponse({"status": "ignored", "reason": reason})
