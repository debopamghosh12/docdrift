"""Web actions (POST): start jobs, change memory, connect the webhook. Plus job polling.

Every long run is a job in the same store the webhook uses, started after the response is sent;
the page polls `GET /jobs/{id}` for its stage and result. On the public demo, every action that
writes to GitHub requires an admin session, and job starts go through the demo's limits.
"""

from urllib.parse import urlsplit

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from github import GithubException

from driftless.commands import CommandName, parse_command
from driftless.jobs import Enqueued, JobMode
from driftless.trust import valid_ref
from driftless.web.job_panel import job_context
from driftless.web.pages import memory_context, setup_context
from driftless.web.state import WebState

WEB_USER = "web"  # created_by of memory rules added from the web app
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def action_routes(state: WebState) -> APIRouter:
    router = APIRouter(default_response_class=HTMLResponse, dependencies=[Depends(same_origin)])

    @router.get("/jobs/{job_id}")
    def job(request: Request, job_id: str) -> HTMLResponse:
        found = state.job(job_id)
        if found is None:
            raise HTTPException(status_code=404, detail="no such job")
        return state.render(request, "partials/job.html", **job_context(found))

    @router.post("/r/{owner}/{name}/check")
    def check(
        request: Request, background: BackgroundTasks, owner: str, name: str, ref: str = Form(""), fix: bool = Form(False)
    ) -> HTMLResponse:
        repo = state.repo(owner, name)
        ref = ref.strip() or repo.default_branch
        if not valid_ref(ref):  # the job then only runs a branch, tag or default-branch commit (trust.py)
            return notice(state, request, "That isn't a branch, tag or commit name.", 400)
        if not state.settings.execute_examples:
            return notice(state, request, "Running examples is turned off on this instance (EXECUTE_EXAMPLES=false).", 409)
        if fix and not (state.settings.github_token and state.settings.llm_api_key):
            return notice(state, request, "Fixing examples needs GITHUB_TOKEN and LLM_API_KEY on the server.", 409)
        if fix:
            state.demo.require_write(request)
        mode = JobMode.FIX_EXAMPLES if fix else JobMode.CHECK
        admit = state.demo.admit(request, repo.full_name)
        return _start(state, request, background, state.store.enqueue(repo.full_name, 0, mode, ref=ref, admit=admit))

    @router.post("/r/{owner}/{name}/pulls/{number}/{action}")
    def pull_action(
        request: Request, background: BackgroundTasks, owner: str, name: str, number: int, action: str
    ) -> HTMLResponse:
        repo = state.repo(owner, name).full_name
        pull = state.browser.pull(repo, number)
        if pull.own:
            return notice(state, request, "This is one of Driftless's own docs PRs; it is never analyzed.", 409)
        store, admit = state.store, state.demo.admit(request, repo)
        if action == "analyze":
            if pull.state == "closed":
                return notice(state, request, "This PR was closed without merging; there is nothing to sync.", 409)
            return _start(state, request, background, store.enqueue(repo, number, JobMode.ANALYZE, admit=admit))
        if action in ("premerge", "docs-pr", "apply"):
            state.demo.require_write(request)
        if action == "premerge" and pull.state == "open":
            enqueued = store.enqueue(repo, number, JobMode.PRE_MERGE, head_sha=pull.head_sha, admit=admit)
            return _start(state, request, background, enqueued)
        if action == "docs-pr" and pull.state == "merged":
            return _start(state, request, background, store.enqueue(repo, number, JobMode.POST_MERGE, admit=admit))
        if action == "apply" and pull.state == "open":
            if not pull.same_repo:
                return notice(state, request, "This PR's branch is in a fork, which Driftless can't push to.", 409)
            login = state.browser.login()
            if not login:
                return notice(state, request, "Applying fixes needs GITHUB_TOKEN on the server.", 409)
            enqueued = store.enqueue(repo, number, JobMode.APPLY, requested_by=login, admit=admit)
            return _start(state, request, background, enqueued)
        return notice(state, request, f"“{action}” isn't available for a {pull.state} pull request.", 409)

    @router.post("/r/{owner}/{name}/memory/{verb}")
    def memory(request: Request, owner: str, name: str, verb: str, value: str = Form("")) -> HTMLResponse:
        repo = state.repo(owner, name)
        state.demo.require_write(request)
        command = parse_command(f"/driftless {verb} {value.strip()}")  # same rules as a PR comment
        if command is None or command.name in (CommandName.APPLY, CommandName.MEMORY):
            message, ok = "Enter a value (at most 300 characters).", False
        else:
            message, ok = _remember(state, repo.full_name, command.name, command.argument)
        ctx = memory_context(state, repo.full_name)
        return state.render(request, "partials/memory_panel.html", repo=repo, message=message, ok=ok, **ctx)

    @router.post("/r/{owner}/{name}/webhook")
    def connect_webhook(request: Request, owner: str, name: str, url: str = Form("")) -> HTMLResponse:
        repo = state.repo(owner, name)
        state.demo.require_write(request)
        url = url.strip()
        ctx = setup_context(state, repo)
        parts = urlsplit(url)
        error = None
        if parts.scheme not in ("http", "https") or not parts.hostname or " " in url:
            error = "Enter the public payload URL, e.g. https://smee.io/<channel> or https://<your-host>/webhook."
        elif not state.settings.webhook_secret:
            error = "Set WEBHOOK_SECRET on the server first: GitHub signs every delivery with it."
        else:
            try:
                hook = state.browser.create_hook(repo.full_name, url, state.settings.webhook_secret)
                ctx.update(setup_context(state, repo))
                return state.render(request, "partials/webhook_panel.html", repo=repo, created=hook, **ctx)
            except GithubException as exc:
                message = exc.data.get("message") if isinstance(exc.data, dict) else str(exc)
                error = f"GitHub refused to create the webhook ({exc.status}: {message})."
        return state.render(request, "partials/webhook_panel.html", repo=repo, error=error, manual=True, **ctx)

    return router


def _remember(state: WebState, repo: str, name: CommandName, argument: str) -> tuple[str, bool]:
    memory = state.memory
    if name == CommandName.IGNORE:
        if memory.add_ignore(repo, argument, WEB_USER):
            return f"Driftless will ignore `{argument}` in this repo.", True
        return f"`{argument}` is already ignored.", False
    if name == CommandName.UNIGNORE:
        if memory.remove_ignore(repo, argument):
            return f"Driftless no longer ignores `{argument}`.", True
        return f"There is no ignore rule `{argument}`.", False
    if name == CommandName.STYLE:
        return f"Noted style preference #{memory.add_style(repo, argument, WEB_USER)}.", True
    removed = memory.remove_style(repo, int(argument)) if argument.isdigit() else None
    if removed is None:
        return f"There is no style preference #{argument}.", False
    return f"Removed style preference #{argument}: “{removed}”.", True


def _start(state: WebState, request: Request, background: BackgroundTasks, enqueued: Enqueued) -> HTMLResponse:
    if enqueued.created:
        background.add_task(state.store.run, enqueued.job)
    ctx = job_context(enqueued.job)
    return state.render(request, "partials/job.html", 202 if enqueued.created else 200, reused=not enqueued.created, **ctx)


def notice(state: WebState, request: Request, message: str, status_code: int) -> HTMLResponse:
    # htmx only swaps 2xx responses into the page; plain requests get the real status.
    status_code = 200 if request.headers.get("HX-Request") else status_code
    return state.render(request, "partials/notice.html", status_code, message=message)


def same_origin(request: Request) -> None:
    """Refuse cross-site form posts: a page elsewhere must not start runs on this server.

    Browsers send Sec-Fetch-Site (and Origin on every POST); a request with neither header isn't
    from a browser, so it can't carry a victim's cookie for CSRF."""
    if request.method in _SAFE_METHODS:
        return
    site = request.headers.get("sec-fetch-site")
    if site is not None and site not in ("same-origin", "none"):
        raise HTTPException(status_code=403, detail="cross-origin request refused")
    origin = request.headers.get("origin") or request.headers.get("referer")
    if origin and urlsplit(origin).netloc != request.headers.get("host", ""):
        raise HTTPException(status_code=403, detail="cross-origin request refused")
