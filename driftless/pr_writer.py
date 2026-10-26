"""Create a branch, commit rewritten docs, and open a PR explaining each change.

Idempotent per source PR: the branch `driftless/pr-<n>` is rebuilt as one commit on top of the
default branch and force-updated, and an existing open docs PR has its body edited rather than
a second PR being opened. When there is nothing to commit, the source PR gets a comment
(edited in place on reruns) instead.
"""

from enum import Enum
from typing import Protocol

from pydantic import BaseModel, Field

from driftless.doc_editor import final_docs
from driftless.pipeline import (
    DRIFTLESS_BRANCH_PREFIX,
    AnalysisResult,
    OwnPullRequestError,
    is_driftless_pull_request,
)
from driftless.execution import ExecutionReport
from driftless.pr_body import has_source_marker, render_check_body, render_pr_body, render_sync_comment
from driftless.proposals import DocProposals


class PullRequestRef(BaseModel):
    number: int
    url: str


class CommentRef(BaseModel):
    id: int
    body: str
    url: str


class DocsRepo(Protocol):
    """The GitHub write operations Driftless needs (implemented by GitHubWriter)."""

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

    def edit_comment(self, repo: str, number: int, comment_id: int, body: str) -> str: ...


class PublishAction(str, Enum):
    OPENED = "opened"
    UPDATED = "updated"
    COMMENTED = "commented"
    COMMITTED = "committed"  # `/driftless apply` pushed doc fixes to the PR's own branch
    DECLINED = "declined"  # `/driftless apply` refused (fork, no write access, closed PR)


class PublishResult(BaseModel):
    action: PublishAction
    url: str
    branch: str | None = None
    files: list[str] = Field(default_factory=list)
    skipped_files: list[str] = Field(default_factory=list)  # changed on the default branch since
    check_state: str | None = None  # pre-merge commit status: success / failure / error
    number: int | None = None  # the docs PR's number, when one was opened or updated
    commit_sha: str | None = None  # the commit `/driftless apply` pushed


CHECK_BRANCH = f"{DRIFTLESS_BRANCH_PREFIX}fix-examples"


def branch_name(number: int) -> str:
    return f"{DRIFTLESS_BRANCH_PREFIX}pr-{number}"


def pull_title(result: AnalysisResult) -> str:
    return f"docs: sync docs with #{result.pr.number} ({result.pr.title})"


class PRWriter:
    def __init__(self, github: DocsRepo) -> None:
        self._gh = github

    def publish(self, result: AnalysisResult, proposals: DocProposals, min_confidence: float) -> PublishResult:
        pr = result.pr
        if is_driftless_pull_request(pr):
            raise OwnPullRequestError(f"{pr.repo}#{pr.number} is a Driftless docs PR; not writing docs for it")

        updated = final_docs(result.docs, proposals, min_confidence)
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
        return PublishResult(
            action=action, url=ref.url, branch=branch, files=sorted(files), skipped_files=skipped, number=ref.number
        )

    def publish_example_fixes(
        self, repo: str, ref: str, originals: dict[str, str], report: ExecutionReport
    ) -> PublishResult | None:
        """Open (or update) one PR with the examples `driftless check --fix` repaired.

        None when nothing changed, or every changed file moved on the default branch since `ref`.
        """
        changed = {p: t for p, t in report.contents.items() if p in originals and t != originals[p]}
        if not changed:
            return None
        base, base_sha = self._gh.default_branch(repo)
        files = {p: t for p, t in changed.items() if self._gh.read_file(repo, p, base_sha) == originals[p]}
        skipped = sorted(set(changed) - set(files))
        if not files:
            return None
        commit = self._gh.commit_files(repo, base_sha, files, "docs: fix examples that fail when run")
        self._gh.set_branch(repo, CHECK_BRANCH, commit)
        title = "docs: fix examples that fail when run"
        body = render_check_body(repo, ref, report, sorted(files))
        existing = self._gh.find_open_pull(repo, CHECK_BRANCH)
        if existing:
            ref_, action = self._gh.update_pull(repo, existing.number, title, body), PublishAction.UPDATED
        else:
            ref_, action = self._gh.create_pull(repo, CHECK_BRANCH, base, title, body), PublishAction.OPENED
        return PublishResult(
            action=action, url=ref_.url, branch=CHECK_BRANCH, files=sorted(files), skipped_files=skipped
        )

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
        comments = self._gh.list_comments(pr.repo, pr.number)
        mine = next((c for c in comments if has_source_marker(c.body, pr.number)), None)
        if mine:
            url = self._gh.edit_comment(pr.repo, pr.number, mine.id, body)
        else:
            url = self._gh.create_comment(pr.repo, pr.number, body)
        return PublishResult(action=PublishAction.COMMENTED, url=url, skipped_files=skipped or [])
