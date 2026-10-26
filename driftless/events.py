"""What a GitHub event asks Driftless to do. Pure: shared by the webhook server and `driftless action`.

- `pull_request` opened / synchronize / reopened / ready_for_review: pre-merge check of the head.
- `pull_request` closed + merged: post-merge sync. Closed on a Driftless docs PR: feedback.
- `issue_comment` "/driftless apply" on an open PR: apply. Other `/driftless` commands: memory.
- `workflow_dispatch` / `schedule` (Actions only): docs health audit of a ref.
"""

from enum import Enum
from typing import Any

from pydantic import BaseModel

from driftless.commands import Command, CommandName, parse_command
from driftless.pipeline import is_own_branch

PRE_MERGE_ACTIONS = frozenset({"opened", "synchronize", "reopened", "ready_for_review"})
AUDIT_EVENTS = frozenset({"workflow_dispatch", "schedule"})


class EventKind(str, Enum):
    PRE_MERGE = "pre-merge"
    POST_MERGE = "post-merge"
    APPLY = "apply"
    MEMORY = "memory"
    DOCS_PR_CLOSED = "docs-pr-closed"  # one of our docs PRs was merged or closed: feedback
    AUDIT = "audit"
    IGNORED = "ignored"


class EventPlan(BaseModel):
    kind: EventKind
    repo: str = ""
    number: int = 0
    head_sha: str = ""
    from_fork: bool = False  # PRE_MERGE: the head branch lives in another repo
    merged: bool = False  # DOCS_PR_CLOSED
    user: str = ""  # who commented (APPLY, MEMORY)
    command: Command | None = None
    comment_id: int | None = None
    ref: str = ""  # AUDIT
    fix: bool = False  # AUDIT: fix broken examples and open a PR
    reason: str = ""  # IGNORED: why


def route_event(
    event: str, payload: dict[str, Any], default_repo: str = "", default_ref: str = "", fix: bool = False
) -> EventPlan:
    """`default_repo` / `default_ref` fill in what `schedule` payloads leave out (GITHUB_REPOSITORY,
    GITHUB_REF_NAME in Actions). `fix` is the caller's choice for audits; a dispatch input can set it too."""
    if event == "pull_request":
        return _pull_request(payload)
    if event == "issue_comment":
        return _issue_comment(payload)
    if event in AUDIT_EVENTS:
        repo = (payload.get("repository") or {}).get("full_name") or default_repo
        ref = _short_ref(payload.get("ref") or "") or default_ref
        if not ref:
            ref = (payload.get("repository") or {}).get("default_branch") or "main"
        wants_fix = fix or str((payload.get("inputs") or {}).get("fix", "")).lower() == "true"
        return EventPlan(kind=EventKind.AUDIT, repo=repo, ref=ref, fix=wants_fix)
    return _ignored(f"event {event!r}")


def _pull_request(payload: dict[str, Any]) -> EventPlan:
    pr = payload.get("pull_request") or {}
    action = payload.get("action")
    repo, number = payload["repository"]["full_name"], int(pr["number"])
    if is_own_branch(pr.get("head", {}).get("ref", "")):
        if action == "closed":
            return EventPlan(kind=EventKind.DOCS_PR_CLOSED, repo=repo, number=number, merged=bool(pr.get("merged")))
        return _ignored("Driftless's own docs PR")
    if action == "closed":
        if not pr.get("merged"):
            return _ignored("pull request was closed without merging")
        return EventPlan(kind=EventKind.POST_MERGE, repo=repo, number=number)
    if action in PRE_MERGE_ACTIONS:
        if pr.get("draft"):
            return _ignored("draft pull request")
        head = pr.get("head") or {}
        head_repo = (head.get("repo") or {}).get("full_name") or ""
        return EventPlan(
            kind=EventKind.PRE_MERGE, repo=repo, number=number, head_sha=head.get("sha", ""),
            from_fork=head_repo.lower() != repo.lower(),
        )
    return _ignored(f"pull_request action {action!r}")


def _issue_comment(payload: dict[str, Any]) -> EventPlan:
    issue, comment = payload.get("issue") or {}, payload.get("comment") or {}
    if payload.get("action") != "created":
        return _ignored("comment was not created")
    if not issue.get("pull_request"):
        return _ignored("comment is not on a pull request")
    command = parse_command(comment.get("body") or "")
    if command is None:
        return _ignored("not a Driftless command")
    # Write access and fork checks need API calls, so the job does them (and replies if refused).
    user = (comment.get("user") or {}).get("login", "")
    repo, number = payload["repository"]["full_name"], int(issue["number"])
    if command.name == CommandName.APPLY:
        if issue.get("state") != "open":
            return _ignored("pull request is closed")
        return EventPlan(kind=EventKind.APPLY, repo=repo, number=number, user=user, command=command)
    return EventPlan(
        kind=EventKind.MEMORY, repo=repo, number=number, user=user, command=command, comment_id=comment.get("id")
    )


def _short_ref(ref: str) -> str:
    for prefix in ("refs/heads/", "refs/tags/"):
        if ref.startswith(prefix):
            return ref.removeprefix(prefix)
    return ref


def _ignored(reason: str) -> EventPlan:
    return EventPlan(kind=EventKind.IGNORED, reason=reason)
