"""FastAPI webhook server: a merged PR on a watched repo triggers a background docs sync."""

import hashlib
import hmac
import json
import logging
from functools import partial
from typing import Any

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from docdrift.config import Settings, get_settings
from docdrift.jobs import Job, JobStore, SyncFn
from docdrift.pipeline import DOCDRIFT_BRANCH_PREFIX

logger = logging.getLogger("docdrift.server")


def verify_signature(secret: str, body: bytes, signature: str | None) -> bool:
    """Check GitHub's `X-Hub-Signature-256: sha256=<hex>` header. No secret configured = reject."""
    if not secret or not signature or not signature.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature.removeprefix("sha256="))


def create_app(settings: Settings | None = None, sync: SyncFn | None = None) -> FastAPI:
    settings = settings or get_settings()
    if sync is None:
        from docdrift.sync import sync_pull_request

        sync = partial(sync_pull_request, settings)
    if not settings.webhook_secret:
        logger.warning("WEBHOOK_SECRET is not set; every webhook delivery will be rejected")

    store = JobStore(sync)
    app = FastAPI(title="DocDrift")
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
        if x_github_event != "pull_request":
            return _ignored(f"event {x_github_event!r}")

        pr = payload.get("pull_request") or {}
        if payload.get("action") != "closed" or not pr.get("merged"):
            return _ignored("pull request was not merged")
        if pr.get("head", {}).get("ref", "").startswith(DOCDRIFT_BRANCH_PREFIX):
            return _ignored("DocDrift's own docs PR")

        repo, number = payload["repository"]["full_name"], int(pr["number"])
        job, created = store.enqueue(repo, number)
        if not created:
            return JSONResponse({"status": "already running", "job": job.model_dump(mode="json")})
        background.add_task(store.run, job)
        return JSONResponse({"status": "queued", "job": job.model_dump(mode="json")}, status_code=202)

    return app


def _ignored(reason: str) -> JSONResponse:
    logger.info("ignored webhook: %s", reason)
    return JSONResponse({"status": "ignored", "reason": reason})
