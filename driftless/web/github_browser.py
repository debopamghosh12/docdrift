"""What the web pages read from GitHub: repo info, doc files, recent PRs, webhooks.

Reads are cached briefly so switching tabs doesn't cost a round of API calls each time.
"""

import threading
import time
from itertools import islice
from collections.abc import Callable
from datetime import datetime
from typing import Protocol, TypeVar
from urllib.parse import urlsplit

from github import Auth, Github
from github.Hook import Hook
from github.PullRequest import PullRequest
from pydantic import BaseModel

from driftless.doc_index import is_doc_path
from driftless.pipeline import is_own_branch

T = TypeVar("T")
WEBHOOK_EVENTS = ("pull_request", "issue_comment")


class RepoSummary(BaseModel):
    full_name: str
    description: str = ""
    default_branch: str
    url: str
    private: bool = False


class PullSummary(BaseModel):
    number: int
    title: str
    state: str  # open | merged | closed
    url: str
    author: str = ""
    head_ref: str = ""
    head_sha: str = ""
    same_repo: bool = True  # the head branch lives in this repo (not a fork)
    draft: bool = False
    updated_at: datetime | None = None

    @property
    def own(self) -> bool:
        """One of Driftless's own docs PRs: never analyzed."""
        return is_own_branch(self.head_ref)


class HookSummary(BaseModel):
    id: int
    host: str  # only the payload URL's host is shown: the full URL can be a secret channel
    events: list[str]
    active: bool
    sends_json: bool  # content type application/json

    @property
    def for_driftless(self) -> bool:
        """Delivers what Driftless needs: pull requests and issue comments, as JSON."""
        events = set(self.events)
        return self.active and self.sends_json and ("*" in events or events.issuperset(WEBHOOK_EVENTS))


class RepoBrowser(Protocol):
    def repo(self, name: str) -> RepoSummary: ...

    def doc_files(self, name: str, ref: str) -> list[str]: ...

    def pulls(self, name: str, limit: int = 10) -> list[PullSummary]:
        """Open PRs, then merged ones, most recently updated first."""
        ...

    def pull(self, name: str, number: int) -> PullSummary: ...

    def hooks(self, name: str) -> list[HookSummary]: ...

    def create_hook(self, name: str, url: str, secret: str) -> HookSummary: ...

    def login(self) -> str:
        """The account GITHUB_TOKEN belongs to ("" without a token)."""
        ...


class GitHubBrowser:
    def __init__(self, token: str | None, ttl_s: float = 60.0) -> None:
        self._gh = Github(auth=Auth.Token(token)) if token else Github()
        self._has_token = bool(token)
        self._ttl = ttl_s
        self._cache: dict[tuple[str, ...], tuple[float, object]] = {}
        self._lock = threading.Lock()

    def repo(self, name: str) -> RepoSummary:
        def load() -> RepoSummary:
            r = self._gh.get_repo(name)
            return RepoSummary(
                full_name=r.full_name, description=r.description or "", default_branch=r.default_branch,
                url=r.html_url, private=r.private,
            )

        return self._cached(("repo", name.lower()), load)

    def doc_files(self, name: str, ref: str) -> list[str]:
        def load() -> list[str]:
            tree = self._gh.get_repo(name).get_git_tree(ref, recursive=True).tree
            return sorted(e.path for e in tree if e.type == "blob" and is_doc_path(e.path))

        return self._cached(("docs", name.lower(), ref), load)

    def pulls(self, name: str, limit: int = 10) -> list[PullSummary]:
        def load() -> list[PullSummary]:
            r = self._gh.get_repo(name)
            # islice, not [:n]: PaginatedList slicing raises IndexError past the last item.
            opened = [_pull(name, p) for p in islice(r.get_pulls(state="open", sort="updated", direction="desc"), limit)]
            closed = islice(r.get_pulls(state="closed", sort="updated", direction="desc"), limit * 3)
            merged = [_pull(name, p) for p in closed if p.merged_at is not None][:limit]
            return opened + merged

        return self._cached(("pulls", name.lower(), str(limit)), load, ttl=20.0)

    def pull(self, name: str, number: int) -> PullSummary:
        return self._cached(("pull", name.lower(), str(number)), lambda: _pull(name, self._gh.get_repo(name).get_pull(number)), ttl=10.0)

    def hooks(self, name: str) -> list[HookSummary]:
        return [_hook(h) for h in self._gh.get_repo(name).get_hooks()]

    def create_hook(self, name: str, url: str, secret: str) -> HookSummary:
        config = {"url": url, "content_type": "json", "secret": secret, "insecure_ssl": "0"}
        hook = self._gh.get_repo(name).create_hook("web", config, events=list(WEBHOOK_EVENTS), active=True)
        return _hook(hook)

    def login(self) -> str:
        if not self._has_token:
            return ""
        return self._cached(("login",), lambda: self._gh.get_user().login, ttl=3600.0)

    def _cached(self, key: tuple[str, ...], load: Callable[[], T], ttl: float | None = None) -> T:
        now = time.monotonic()
        with self._lock:
            hit = self._cache.get(key)
        if hit and now - hit[0] < (ttl if ttl is not None else self._ttl):
            return hit[1]  # type: ignore[return-value]
        value = load()
        with self._lock:
            self._cache[key] = (now, value)
        return value


def _pull(repo: str, p: PullRequest) -> PullSummary:
    state = "open" if p.state == "open" else ("merged" if p.merged_at is not None else "closed")
    head_repo = p.head.repo.full_name if p.head.repo else ""
    return PullSummary(
        number=p.number, title=p.title, state=state, url=p.html_url, author=p.user.login if p.user else "",
        head_ref=p.head.ref, head_sha=p.head.sha, same_repo=head_repo.lower() == repo.lower(),
        draft=bool(p.draft), updated_at=p.updated_at,
    )


def _hook(hook: Hook) -> HookSummary:
    config = hook.config or {}
    return HookSummary(
        id=hook.id,
        host=urlsplit(str(config.get("url", ""))).hostname or "?",
        events=list(hook.events or []),
        active=bool(hook.active),
        sends_json=config.get("content_type") == "json",
    )
