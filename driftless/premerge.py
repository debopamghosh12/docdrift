"""Pre-merge checks: report doc drift on an open PR, and commit the fixes on `/driftless apply`.

One comment per PR (found by a hidden marker, edited in place on every push) and one commit
status on the head SHA. `/driftless apply` commits exactly the verified fixes the comment showed
for the current head, on top of it, to the PR's own branch: never to a fork, and only for
users with write access.
"""

from collections.abc import Callable
from typing import Protocol

from pydantic import BaseModel, Field

from driftless.doc_editor import final_docs
from driftless.doc_index import is_safe_doc_path
from driftless.pipeline import AnalysisResult
from driftless.pr_writer import CommentRef, PublishAction, PublishResult
from driftless.premerge_body import (
    fixes_marker,
    parse_fixes_marker,
    premerge_marker,
    render_apply_declined,
    render_apply_reply,
    render_premerge_comment,
    summarize,
)
from driftless.proposals import DocProposals

STATUS_CONTEXT = "Driftless / docs-in-sync"
WRITE_PERMISSIONS = frozenset({"admin", "maintain", "write"})


class SupersededError(RuntimeError):
    """A newer push arrived for the same PR; this run's results would be out of date."""


class PullState(BaseModel):
    number: int
    open: bool
    head_ref: str
    head_sha: str
    head_repo: str | None  # None when the fork was deleted
    base_repo: str

    @property
    def from_fork(self) -> bool:
        return (self.head_repo or "").lower() != self.base_repo.lower()


class PendingFixes(BaseModel):
    """What `/driftless apply` would commit for one head SHA."""

    head_sha: str
    files: dict[str, str] = Field(default_factory=dict)  # path -> verified new content


class CheckRepo(Protocol):
    """The GitHub operations pre-merge checks need (implemented by GitHubWriter)."""

    def list_comments(self, repo: str, number: int) -> list[CommentRef]: ...

    def create_comment(self, repo: str, number: int, body: str) -> str: ...

    def edit_comment(self, repo: str, number: int, comment_id: int, body: str) -> str: ...

    def set_status(self, repo: str, sha: str, state: str, description: str, target_url: str | None = None) -> None: ...

    def pull_state(self, repo: str, number: int) -> PullState: ...

    def permission(self, repo: str, user: str) -> str:
        """admin / maintain / write / triage / read / none."""
        ...

    def commit_files(self, repo: str, parent_sha: str, files: dict[str, str], message: str) -> str: ...

    def read_file(self, repo: str, path: str, ref: str) -> str | None: ...

    def advance_branch(self, repo: str, branch: str, sha: str) -> None:
        """Fast-forward `branch` to `sha`; fails if someone pushed in between."""
        ...


class PreMergeChecker:
    def __init__(self, github: CheckRepo) -> None:
        self._gh = github

    def start(self, repo: str, sha: str) -> None:
        self._gh.set_status(repo, sha, "pending", "Checking the docs against this commit...")

    def fail(self, repo: str, sha: str, error: Exception) -> None:
        self._gh.set_status(repo, sha, "error", f"Driftless failed: {type(error).__name__}"[:140])

    def report(
        self, result: AnalysisResult, proposals: DocProposals, min_confidence: float
    ) -> tuple[PublishResult, PendingFixes]:
        """Post or edit the PR's one Driftless comment and set the head commit's status."""
        pr = result.pr
        fixes = final_docs(result.docs, proposals, min_confidence)
        body = render_premerge_comment(result, proposals, min_confidence, fixes)
        if fixes:
            # Keep exactly these fixes in a commit on no branch, so `/driftless apply` can commit them
            # later from any process (a fresh Actions runner has no cache) without asking the LLM again.
            stash = self._gh.commit_files(pr.repo, pr.head_sha, fixes, f"Driftless: proposed doc fixes for #{pr.number}")
            body += "\n" + fixes_marker(pr.head_sha, stash, sorted(fixes)) + "\n"
        mine = self._comment(pr.repo, pr.number)
        url = self._gh.edit_comment(pr.repo, pr.number, mine.id, body) if mine else self._gh.create_comment(pr.repo, pr.number, body)

        check = summarize(proposals, min_confidence)
        self._gh.set_status(pr.repo, pr.head_sha, check.state, check.description, url)
        published = PublishResult(
            action=PublishAction.COMMENTED, url=url, files=sorted(fixes), check_state=check.state
        )
        return published, PendingFixes(head_sha=pr.head_sha, files=fixes)

    def stored_fixes(self, repo: str, number: int, head_sha: str) -> PendingFixes | None:
        """The fixes the PR's comment shows for `head_sha`, read back from the commit it points at."""
        mine = self._comment(repo, number)
        stored = parse_fixes_marker(mine.body) if mine else None
        if stored is None or stored[0] != head_sha:
            return None
        _, commit, paths = stored
        if not all(is_safe_doc_path(path) for path in paths):
            return None  # only doc files are ever committed; re-check instead
        files: dict[str, str] = {}
        for path in paths:
            try:
                text = self._gh.read_file(repo, path, commit)
            except Exception:  # noqa: BLE001 - e.g. the commit was garbage-collected: re-check instead
                return None
            if text is None:
                return None
            files[path] = text
        return PendingFixes(head_sha=head_sha, files=files)

    def _comment(self, repo: str, number: int) -> CommentRef | None:
        """Our comment: has the marker *and* was posted by a bot or someone with write access.
        Anyone can paste the marker into a comment of their own, pointing `/driftless apply` at a
        commit of theirs; such a comment is ignored."""
        return next(
            (
                c for c in self._gh.list_comments(repo, number)
                if premerge_marker(number) in c.body
                and (c.bot or (c.author and self._gh.permission(repo, c.author) in WRITE_PERMISSIONS))
            ),
            None,
        )

    def apply(
        self, repo: str, number: int, user: str, fixes_for: Callable[[PullState], PendingFixes]
    ) -> PublishResult:
        """Commit the verified fixes for the PR's current head to its branch.

        `fixes_for` returns the fixes computed at `pull.head_sha` (cached, or a fresh check).
        """
        if self._gh.permission(repo, user) not in WRITE_PERMISSIONS:
            return self._decline(repo, number, user, "only users with write access to this repository can apply them.")
        pull = self._gh.pull_state(repo, number)
        if not pull.open:
            return self._decline(repo, number, user, "this pull request is closed.")
        if pull.from_fork:
            return self._decline(
                repo, number, user, "this PR's branch is in a fork, which Driftless can't push to. Apply the diff by hand."
            )

        fixes = fixes_for(pull)
        if not fixes.files:
            url = self._gh.create_comment(
                repo, number, f"✅ @{user} Nothing to apply: the docs are in sync with `{pull.head_sha[:7]}`."
            )
            return PublishResult(action=PublishAction.COMMENTED, url=url)

        message = f"docs: sync docs with this PR\n\nApplied by Driftless (`/driftless apply` from @{user})."
        sha = self._gh.commit_files(repo, pull.head_sha, fixes.files, message)
        self._gh.advance_branch(repo, pull.head_ref, sha)
        commit_url = f"https://github.com/{repo}/commit/{sha}"
        files = sorted(fixes.files)
        self._gh.create_comment(repo, number, render_apply_reply(user, pull.head_ref, commit_url, files))
        return PublishResult(
            action=PublishAction.COMMITTED, url=commit_url, branch=pull.head_ref, files=files, commit_sha=sha
        )

    def _decline(self, repo: str, number: int, user: str, reason: str) -> PublishResult:
        url = self._gh.create_comment(repo, number, render_apply_declined(user, reason))
        return PublishResult(action=PublishAction.DECLINED, url=url)
