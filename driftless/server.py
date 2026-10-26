"""FastAPI webhook server for a watched repo.

- `pull_request` opened / synchronize / reopened / ready_for_review: pre-merge check of the head
  commit (one PR comment, edited in place, and a commit status).
- `pull_request` closed + merged: post-merge sync (docs PR, or an in-sync comment).
- `issue_comment` "/driftless apply" on an open PR: commit the verified doc fixes to its branch.
"""

import hashlib
import hmac
import json
import logging
from typing import Any

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from driftless.config import Settings, get_settings
from driftless.jobs import Enqueued, Job, JobMode, JobStore, Syncer
from driftless.pipeline import is_own_branch
from driftless.premerge_body import APPLY_COMMAND

logger = logging.getLogger("driftless.server")

PRE_MERGE_ACTIONS = frozenset({"opened", "synchronize", "reopened", "ready_for_review"})


def verify_signature(secret: str, body: bytes, signature: str | None) -> bool:
    """Check GitHub's `X-Hub-Signature-256: sha256=<hex>` header. No secret configured = reject."""
    if not secret or not signature or not signature.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature.removeprefix("sha256="))


def create_app(settings: Settings | None = None, syncer: Syncer | None = None) -> FastAPI:
    settings = settings or get_settings()
    if syncer is None:
        from driftless.sync import GitHubSyncer

        syncer = GitHubSyncer(settings)
    if not settings.webhook_secret:
        logger.warning("WEBHOOK_SECRET is not set; every webhook delivery will be rejected")

    store = JobStore(syncer)
    app = FastAPI(title="Driftless")
    app.state.jobs = store

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/runs")
    def runs() -> list[Job]:
        return store.recent()

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
        if x_github_event == "pull_request":
            return _pull_request(store, background, payload)
        if x_github_event == "issue_comment":
            return _issue_comment(store, background, payload)
        return _ignored(f"event {x_github_event!r}")

    return app


def _pull_request(store: JobStore, background: BackgroundTasks, payload: dict[str, Any]) -> JSONResponse:
    pr = payload.get("pull_request") or {}
    action = payload.get("action")
    if is_own_branch(pr.get("head", {}).get("ref", "")):
        return _ignored("Driftless's own docs PR")
    repo, number = payload["repository"]["full_name"], int(pr["number"])
    if action == "closed":
        if not pr.get("merged"):
            return _ignored("pull request was closed without merging")
        return _queue(store, background, store.enqueue(repo, number, JobMode.POST_MERGE))
    if action in PRE_MERGE_ACTIONS:
        if pr.get("draft"):
            return _ignored("draft pull request")
        head_sha = pr.get("head", {}).get("sha", "")
        return _queue(store, background, store.enqueue(repo, number, JobMode.PRE_MERGE, head_sha=head_sha))
    return _ignored(f"pull_request action {action!r}")


def _issue_comment(store: JobStore, background: BackgroundTasks, payload: dict[str, Any]) -> JSONResponse:
    issue, comment = payload.get("issue") or {}, payload.get("comment") or {}
    if payload.get("action") != "created":
        return _ignored("comment was not created")
    if not issue.get("pull_request"):
        return _ignored("comment is not on a pull request")
    if (comment.get("body") or "").strip() != APPLY_COMMAND:
        return _ignored("not a Driftless command")
    if issue.get("state") != "open":
        return _ignored("pull request is closed")
    # Write access and fork checks need API calls, so the job does them (and replies if refused).
    user = (comment.get("user") or {}).get("login", "")
    enqueued = store.enqueue(payload["repository"]["full_name"], int(issue["number"]), JobMode.APPLY, requested_by=user)
    return _queue(store, background, enqueued)


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
