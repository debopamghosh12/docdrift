"""Create a branch, commit rewritten docs, and open a PR explaining each change.

Idempotent per source PR: the branch `docdrift/pr-<n>` is rebuilt as one commit on top of the
default branch and force-updated, and an existing open docs PR has its body edited rather than
a second PR being opened. When there is nothing to commit, the source PR gets a comment
(edited in place on reruns) instead.
"""

from enum import Enum
from typing import Protocol

from pydantic import BaseModel, Field

from docdrift.doc_editor import apply_proposals
from docdrift.pipeline import (
    DOCDRIFT_BRANCH_PREFIX,
    AnalysisResult,
    OwnPullRequestError,
    is_docdrift_pull_request,
)
from docdrift.pr_body import render_pr_body, render_sync_comment, source_marker
from docdrift.proposals import DocProposals


class PullRequestRef(BaseModel):
    number: int
    url: str


class CommentRef(BaseModel):
    id: int
    body: str
    url: str


class DocsRepo(Protocol):
    """The GitHub write operations DocDrift needs (implemented by GitHubWriter)."""

    def default_branch(self, repo: str) -> tuple[str, str]:
        """(branch name, head commit sha)."""
        ...

    def read_file(self, repo: str, path: str, ref: str) -> str | None: ...

    def commit_files(self, repo: str, parent_sha: str, files: dict[str, str], message: str) -> str:
        """Create one commit on top of `parent_sha` with these file contents; return its sha."""
        ...

    def set_branch(self, repo: str, branch: str, sha: str) -> None:
        """Create `branch` at `sha`, or force-move it there if it exists."""
        ...

    def find_open_pull(self, repo: str, head_branch: str) -> PullRequestRef | None: ...

    def create_pull(self, repo: str, head: str, base: str, title: str, body: str) -> PullRequestRef: ...

    def update_pull(self, repo: str, number: int, title: str, body: str) -> PullRequestRef: ...

    def list_comments(self, repo: str, number: int) -> list[CommentRef]: ...

    def create_comment(self, repo: str, number: int, body: str) -> str: ...

    def edit_comment(self, repo: str, comment_id: int, body: str) -> str: ...


class PublishAction(str, Enum):
    OPENED = "opened"
    UPDATED = "updated"
    COMMENTED = "commented"


class PublishResult(BaseModel):
    action: PublishAction
    url: str
    branch: str | None = None
    files: list[str] = Field(default_factory=list)
    skipped_files: list[str] = Field(default_factory=list)  # changed on the default branch since


def branch_name(number: int) -> str:
    return f"{DOCDRIFT_BRANCH_PREFIX}pr-{number}"


def pull_title(result: AnalysisResult) -> str:
    return f"docs: sync docs with #{result.pr.number} ({result.pr.title})"


class PRWriter:
    def __init__(self, github: DocsRepo) -> None:
        self._gh = github

    def publish(self, result: AnalysisResult, proposals: DocProposals, min_confidence: float) -> PublishResult:
        pr = result.pr
        if is_docdrift_pull_request(pr):
            raise OwnPullRequestError(f"{pr.repo}#{pr.number} is a DocDrift docs PR; not writing docs for it")

        updated = apply_proposals(result.docs, proposals, min_confidence)
        if not updated:
            return self._comment(result, render_sync_comment(result, proposals, min_confidence))

        base, base_sha = self._gh.default_branch(pr.repo)
        files, skipped = self._unchanged_since_merge(result, updated, base_sha)
        if not files:
            body = render_sync_comment(result, proposals, min_confidence, skipped)
            return self._comment(result, body, skipped)

        branch = branch_name(pr.number)
        commit = self._gh.commit_files(pr.repo, base_sha, files, f"docs: sync with #{pr.number}")
        self._gh.set_branch(pr.repo, branch, commit)

        title = pull_title(result)
        body = render_pr_body(result, proposals, min_confidence, sorted(files), skipped)
        existing = self._gh.find_open_pull(pr.repo, branch)
        if existing:
            ref, action = self._gh.update_pull(pr.repo, existing.number, title, body), PublishAction.UPDATED
        else:
            ref, action = self._gh.create_pull(pr.repo, branch, base, title, body), PublishAction.OPENED
        return PublishResult(action=action, url=ref.url, branch=branch, files=sorted(files), skipped_files=skipped)

    def _unchanged_since_merge(
        self, result: AnalysisResult, updated: dict[str, str], base_sha: str
    ) -> tuple[dict[str, str], list[str]]:
        """Keep files whose default-branch content is still what we edited (at the merge commit).

        Writing our version over a file someone changed since would silently revert their work.
        """
        originals = {d.path: d.content for d in result.docs}
        files: dict[str, str] = {}
        skipped: list[str] = []
        for path, content in sorted(updated.items()):
            if self._gh.read_file(result.pr.repo, path, base_sha) == originals[path]:
                files[path] = content
            else:
                skipped.append(path)
        return files, skipped

    def _comment(self, result: AnalysisResult, body: str, skipped: list[str] | None = None) -> PublishResult:
        pr = result.pr
        marker = source_marker(pr.number)
        mine = next((c for c in self._gh.list_comments(pr.repo, pr.number) if marker in c.body), None)
        if mine:
            url = self._gh.edit_comment(pr.repo, mine.id, body)
        else:
            url = self._gh.create_comment(pr.repo, pr.number, body)
        return PublishResult(action=PublishAction.COMMENTED, url=url, skipped_files=skipped or [])
