"""Web pages (GET): home, and the repo dashboard's tabs."""

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from github import GithubException

from driftless.jobs import REPO_MODES, JobMode
from driftless.web.access import parse_repo
from driftless.web.github_browser import RepoSummary
from driftless.web.job_panel import job_context
from driftless.web.state import WebState

TABS = (("health", "Docs health"), ("pulls", "Pull requests"), ("memory", "Memory"), ("activity", "Activity"), ("setup", "Setup"))
PR_MODES = frozenset({JobMode.ANALYZE, JobMode.PRE_MERGE, JobMode.POST_MERGE, JobMode.APPLY})


def page_routes(state: WebState) -> APIRouter:
    router = APIRouter(default_response_class=HTMLResponse)

    @router.get("/")
    def home(request: Request) -> HTMLResponse:
        return state.render(request, "home.html", allowed=state.allowed.exact)

    @router.get("/open")
    def open_repo(request: Request, repo: str = "") -> Response:
        parsed = parse_repo(repo)
        if parsed is None:
            error = "Enter a repository as owner/name, e.g. debopamghosh12/driftless-demo."
            return state.render(request, "home.html", 400, allowed=state.allowed.exact, error=error, value=repo)
        return RedirectResponse(f"/r/{parsed[0]}/{parsed[1]}", status_code=303)

    @router.get("/r/{owner}/{name}")
    def health(request: Request, owner: str, name: str) -> HTMLResponse:
        ctx = _dashboard(state, owner, name, "health")
        latest = next((j for j in state.store.recent(ctx["repo"].full_name) if j.mode in REPO_MODES), None)
        ctx["latest"] = job_context(latest) if latest else None
        return state.render(request, "repo/health.html", **ctx)

    @router.get("/r/{owner}/{name}/pulls")
    def pulls(request: Request, owner: str, name: str) -> HTMLResponse:
        ctx = _dashboard(state, owner, name, "pulls")
        ctx["pulls"] = state.browser.pulls(ctx["repo"].full_name)
        return state.render(request, "repo/pulls.html", **ctx)

    @router.get("/r/{owner}/{name}/pulls/{number}")
    def pull(request: Request, owner: str, name: str, number: int) -> HTMLResponse:
        ctx = _dashboard(state, owner, name, "pulls")
        repo = ctx["repo"].full_name
        ctx["pull"] = state.browser.pull(repo, number)
        runs = [j for j in state.store.recent(repo) if j.pr == number and j.mode in PR_MODES]
        analysis = next((j for j in runs if j.mode == JobMode.ANALYZE), None)
        ctx["analysis_job"] = job_context(analysis) if analysis else None
        ctx["runs"] = [j for j in runs if j.mode != JobMode.ANALYZE]
        return state.render(request, "repo/pull.html", **ctx)

    @router.get("/r/{owner}/{name}/memory")
    def memory(request: Request, owner: str, name: str) -> HTMLResponse:
        ctx = _dashboard(state, owner, name, "memory")
        ctx.update(memory_context(state, ctx["repo"].full_name))
        return state.render(request, "repo/memory.html", **ctx)

    @router.get("/r/{owner}/{name}/activity")
    def activity(request: Request, owner: str, name: str) -> HTMLResponse:
        ctx = _dashboard(state, owner, name, "activity")
        ctx["jobs"] = state.store.recent(ctx["repo"].full_name)
        return state.render(request, "repo/activity.html", **ctx)

    @router.get("/r/{owner}/{name}/activity/rows")
    def activity_rows(request: Request, owner: str, name: str) -> HTMLResponse:
        repo = state.repo(owner, name)
        return state.render(request, "partials/activity_rows.html", repo=repo, jobs=state.store.recent(repo.full_name))

    @router.get("/r/{owner}/{name}/setup")
    def setup(request: Request, owner: str, name: str) -> HTMLResponse:
        ctx = _dashboard(state, owner, name, "setup")
        ctx.update(setup_context(state, ctx["repo"]))
        return state.render(request, "repo/setup.html", **ctx)

    return router


def _dashboard(state: WebState, owner: str, name: str, tab: str) -> dict[str, Any]:
    repo = state.repo(owner, name)
    try:
        docs = state.browser.doc_files(repo.full_name, repo.default_branch)
    except GithubException:  # an empty repo has no tree
        docs = []
    return {
        "repo": repo,
        "docs": docs,
        "last_runs": state.store.recent(repo.full_name, limit=4),
        "tab": tab,
        "tabs": TABS,
        "execute_examples": state.settings.execute_examples,
    }


def memory_context(state: WebState, repo: str) -> dict[str, Any]:
    return {
        "ignore_rules": state.memory.ignore_rules(repo),
        "style_notes": state.memory.style_notes(repo),
        "feedback": state.memory.feedback_counts(repo),
        "rejected": state.memory.recent_rejected(repo),
    }


def setup_context(state: WebState, repo: RepoSummary) -> dict[str, Any]:
    settings = state.settings
    hooks, hooks_error = [], None
    try:
        hooks = state.browser.hooks(repo.full_name)
    except GithubException as exc:
        hooks_error = f"GitHub returned {exc.status}: the token can't read this repo's webhooks (it needs Webhooks: read)."
    return {
        "configured": {
            "GITHUB_TOKEN": bool(settings.github_token),
            "WEBHOOK_SECRET": bool(settings.webhook_secret),
            "LLM_API_KEY": bool(settings.llm_api_key),
        },
        "hooks": hooks,
        "hooks_error": hooks_error,
        "connected": any(h.for_driftless for h in hooks),
    }
