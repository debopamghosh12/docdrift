"""Driftless web app: every CLI feature from the browser, served by the webhook server.

Jinja2 templates + htmx + Tailwind (CDN), no build step. Long runs are jobs in the server's
JobStore; pages poll them. Only repos on ALLOWED_REPOS are served, since every action uses the
server's GITHUB_TOKEN. With PUBLIC_DEMO, writes to GitHub need an admin session (`demo.py`).
"""

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from github import GithubException

from driftless.config import Settings
from driftless.jobs import JobRefused, JobStore
from driftless.memory import Memory
from driftless.web.access import AllowedRepos
from driftless.web.actions import action_routes, notice
from driftless.web.admin import admin_routes
from driftless.web.demo import ADMIN_ONLY, AdminRequired, DemoGuard
from driftless.web.github_browser import GitHubBrowser, RepoBrowser
from driftless.web.pages import page_routes
from driftless.web.state import NotEnabled, WebState, make_templates


def install_web(
    app: FastAPI, settings: Settings, store: JobStore, memory: Memory, browser: RepoBrowser | None = None
) -> WebState:
    state = WebState(
        settings=settings,
        store=store,
        memory=memory,
        browser=browser or GitHubBrowser(settings.github_token or None),
        templates=make_templates(settings),
        allowed=AllowedRepos(settings.allowed_repos),
        demo=DemoGuard(settings),
    )
    app.include_router(page_routes(state))
    app.include_router(action_routes(state))
    app.include_router(admin_routes(state))

    @app.exception_handler(AdminRequired)
    def admin_required(request: Request, exc: AdminRequired) -> HTMLResponse:
        return notice(state, request, f"{ADMIN_ONLY}: this writes to GitHub. Log in under Admin to use it.", 403)

    @app.exception_handler(JobRefused)
    def job_refused(request: Request, exc: JobRefused) -> HTMLResponse:
        return notice(state, request, str(exc), 429)

    @app.exception_handler(NotEnabled)
    def not_enabled(request: Request, exc: NotEnabled) -> HTMLResponse:
        return state.render(request, "blocked.html", 403, repo=exc.repo)

    @app.exception_handler(GithubException)
    def github_error(request: Request, exc: GithubException) -> HTMLResponse:
        if exc.status == 404:
            message = "GitHub says this repository or pull request doesn't exist, or the server's token can't see it."
        else:
            detail = exc.data.get("message") if isinstance(exc.data, dict) else ""
            message = f"GitHub returned {exc.status}{f': {detail}' if detail else ''}."
        return state.render(request, "error.html", 404 if exc.status == 404 else 502, message=message)

    return state
