"""Whose code Driftless may run: executing examples starts the repository's app, i.e. runs its code.

Only code that someone with write access put into the repository itself is ever run:

- a merged PR's merge commit (it is in the base branch's history);
- an open PR's head only when its branch lives in the same repository (pushing it needed write
  access) *and* its author has write access; never a fork's head, whoever clicks "Analyze";
- for audits, a branch or tag of the repository (resolved to its commit), or a commit on the
  default branch. Not an arbitrary SHA: GitHub serves a fork's commits through the parent repo's
  API too, so `repos/<parent>/tarball/<fork-sha>` would run the fork's code.

Every check fails closed: an API error means "don't run".
"""

import re
from collections.abc import Callable
from typing import Protocol

from driftless.models import PullRequestInfo

WRITE_PERMISSIONS = frozenset({"admin", "maintain", "write"})
_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
# Git ref names that are also safe in an API URL path: no "..", "//", leading "-" or "/", "@{".
_REF_RE = re.compile(r"^(?!.*\.\.)(?!.*//)(?!.*@\{)(?![-/.])[A-Za-z0-9._/-]{1,200}(?<![/.])$")
_REPO_RE = re.compile(r"^(?!\.\.?/)[A-Za-z0-9_.-]{1,100}/(?!\.\.?$)[A-Za-z0-9_.-]{1,100}$")


class UntrustedCode(RuntimeError):
    """Refused to run code: it isn't known to come from someone with write access."""


class RefLookup(Protocol):
    def branch_sha(self, repo: str, branch: str) -> str | None: ...

    def tag_sha(self, repo: str, tag: str) -> str | None: ...

    def on_default_branch(self, repo: str, sha: str) -> str | None:
        """The full SHA when `sha` is an ancestor of (or is) the default branch's head."""
        ...


def valid_repo_name(repo: str) -> bool:
    return bool(_REPO_RE.match(repo)) and ".." not in repo.split("/")


def valid_ref(ref: str) -> bool:
    return bool(_REF_RE.match(ref)) and not ref.endswith(".lock")


def pull_execution_refusal(pr: PullRequestInfo, permission: Callable[[str, str], str]) -> str | None:
    """Why the examples of this PR's snapshot must not run (None: they may).

    `permission(repo, login)` is GitHub's collaborator permission (admin/maintain/write/...)."""
    if pr.merged and pr.merge_commit_sha:
        return None
    if not pr.head_repo or pr.head_repo.lower() != pr.repo.lower():
        return "this PR's branch is in a fork, and Driftless never runs code from forks"
    try:
        allowed = permission(pr.repo, pr.author) in WRITE_PERMISSIONS
    except Exception:  # noqa: BLE001 - fail closed on any lookup error
        allowed = False
    if not allowed:
        return f"@{pr.author or 'the author'} doesn't have write access to {pr.repo}, so its code isn't run"
    return None


def resolve_trusted_ref(lookup: RefLookup, repo: str, ref: str) -> str:
    """The commit SHA to run for `ref`: a branch or tag of `repo`, or a commit on its default branch."""
    if not valid_repo_name(repo) or not valid_ref(ref):
        raise UntrustedCode(f"{ref!r} isn't a valid branch, tag or commit name")
    for resolve in (lookup.branch_sha, lookup.tag_sha):
        try:
            sha = resolve(repo, ref)
        except Exception:  # noqa: BLE001
            sha = None
        if sha:
            return sha
    if _SHA_RE.match(ref):
        try:
            sha = lookup.on_default_branch(repo, ref)
        except Exception:  # noqa: BLE001
            sha = None
        if sha:
            return sha
        raise UntrustedCode(
            f"commit {ref[:12]} isn't on {repo}'s default branch; only branches, tags and default-branch commits are run"
        )
    raise UntrustedCode(f"{ref!r} isn't a branch or tag of {repo}")
