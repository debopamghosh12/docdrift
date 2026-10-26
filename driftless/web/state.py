"""What every web route needs: settings, the job store, memory, GitHub reads, and templates."""

import re
import time
from dataclasses import dataclass
from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from jinja2 import Environment, PackageLoader, select_autoescape
from markupsafe import Markup, escape

from driftless.config import Settings
from driftless.jobs import Job, JobStore
from driftless.memory import Memory
from driftless.web.access import AllowedRepos, redact, valid_repo
from driftless.web.github_browser import RepoBrowser, RepoSummary

_CODE_RE = re.compile(r"`([^`]+)`")


class NotEnabled(Exception):
    """The repo isn't on ALLOWED_REPOS (or isn't a valid name)."""

    def __init__(self, repo: str) -> None:
        super().__init__(repo)
        self.repo = repo


@dataclass
class WebState:
    settings: Settings
    store: JobStore
    memory: Memory
    browser: RepoBrowser
    templates: Jinja2Templates
    allowed: AllowedRepos

    def repo(self, owner: str, name: str) -> RepoSummary:
        """The repo, if this instance may act on it (raises NotEnabled before any API call)."""
        full = f"{owner}/{name}"
        if not valid_repo(owner, name) or not self.allowed.allows(full):
            raise NotEnabled(full)
        return self.browser.repo(full)

    def job(self, job_id: str) -> Job | None:
        """A job of an allowed repo (others are as good as missing)."""
        job = self.store.get(job_id)
        return job if job is not None and self.allowed.allows(job.repo) else None

    def render(self, request: Request, template: str, status_code: int = 200, **context: Any) -> HTMLResponse:
        return self.templates.TemplateResponse(request, template, context, status_code=status_code)


def make_templates(settings: Settings) -> Jinja2Templates:
    env = Environment(loader=PackageLoader("driftless.web", "templates"), autoescape=select_autoescape(default=True))
    env.filters["code"] = inline_code
    env.filters["redact"] = lambda text: redact(text, settings)
    env.filters["ago"] = ago
    env.filters["duration"] = duration
    return Jinja2Templates(env=env)


def inline_code(text: str | None) -> Markup:
    """Escape, then render `backticks` (as used in Driftless's messages) as <code>."""
    escaped = str(escape(text or ""))
    return Markup(_CODE_RE.sub(r'<code class="code">\1</code>', escaped))


def ago(timestamp: float | None) -> str:
    if not timestamp:
        return ""
    seconds = max(0, int(time.time() - timestamp))
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds // size}{unit} ago"
    return "just now"


def duration(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    if seconds < 60:
        return f"{seconds:.1f}s"
    return f"{int(seconds // 60)}m {int(seconds % 60):02d}s"
