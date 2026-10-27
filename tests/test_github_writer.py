"""GitHubWriter against autospec'd PyGithub classes of the installed version.

Free-form fakes accept any method name, which is how `Repository.get_issue_comment` (absent in
PyGithub 2.x) reached a live run. Here every object is `create_autospec` of the real class, and
each method returns an autospec of the class its return annotation names, so calling a method or
signature that doesn't exist fails the test.
"""

import importlib
import inspect
from typing import Any
from unittest.mock import create_autospec

import pytest
from github import GithubException
from github.Branch import Branch
from github.Commit import Commit
from github.GitCommit import GitCommit
from github.GitRef import GitRef
from github.MainClass import Github
from github.Issue import Issue
from github.IssueComment import IssueComment
from github.NamedUser import NamedUser
from github.PullRequest import PullRequest
from github.PullRequestPart import PullRequestPart
from github.Repository import Repository

import driftless.github_writer
from driftless.github_writer import GitHubWriter
from driftless.premerge import STATUS_CONTEXT


class Mocks:
    """One autospec'd instance per PyGithub class, wired through return annotations on demand."""

    def __init__(self) -> None:
        self._mocks: dict[type, Any] = {}

    def __getitem__(self, cls: type) -> Any:
        if cls not in self._mocks:
            mock = create_autospec(cls, instance=True)
            self._mocks[cls] = mock
            for name, func in inspect.getmembers(cls, inspect.isfunction):
                returned = _returned_class(func)
                if not name.startswith("_") and returned is not None:
                    getattr(mock, name).side_effect = lambda *a, _cls=returned, **k: self[_cls]
        return self._mocks[cls]


def _returned_class(func: Any) -> type | None:
    """`-> 'IssueComment'` resolves to github.IssueComment.IssueComment (PyGithub's module layout)."""
    annotation = func.__annotations__.get("return")
    if isinstance(annotation, type):
        return annotation if annotation.__module__.startswith("github.") else None
    if not isinstance(annotation, str) or not annotation.isidentifier():
        return None  # PaginatedList[...], unions, None
    try:
        value = getattr(importlib.import_module(f"github.{annotation}"), annotation)
    except (ImportError, AttributeError):
        return None
    return value if isinstance(value, type) else None


@pytest.fixture
def gh(monkeypatch: pytest.MonkeyPatch) -> Mocks:
    mocks = Mocks()
    monkeypatch.setattr(driftless.github_writer, "Github", lambda auth: mocks[Github])
    return mocks


def writer() -> GitHubWriter:
    return GitHubWriter("token")


def test_edit_comment_goes_through_the_issue(gh: Mocks) -> None:
    gh[IssueComment].html_url = "https://github.com/me/app/pull/7#issuecomment-5"
    url = writer().edit_comment("me/app", 7, 5, "new body")
    gh[Repository].get_issue.assert_called_with(7)
    gh[Issue].get_comment.assert_called_once_with(5)
    gh[IssueComment].edit.assert_called_once_with("new body")
    assert url == "https://github.com/me/app/pull/7#issuecomment-5"


def test_comments(gh: Mocks) -> None:
    comment = gh[IssueComment]
    comment.id, comment.body, comment.html_url = 3, "hi", "u"
    comment.user.login, comment.user.type = "github-actions[bot]", "Bot"
    gh[Issue].get_comments.side_effect = lambda: [comment]
    listed = writer().list_comments("me/app", 7)
    assert [(c.id, c.body, c.author, c.bot) for c in listed] == [(3, "hi", "github-actions[bot]", True)]
    writer().create_comment("me/app", 7, "body")
    gh[Issue].create_comment.assert_called_once_with("body")


def test_commit_status(gh: Mocks) -> None:
    writer().set_status("me/app", "abc", "failure", "x" * 200, "https://comment")
    gh[Repository].get_commit.assert_called_once_with("abc")
    gh[Commit].create_status.assert_called_once_with(
        state="failure", description="x" * 140, context=STATUS_CONTEXT, target_url="https://comment"
    )


def test_pull_state_and_permission(gh: Mocks) -> None:
    pull, part = gh[PullRequest], gh[PullRequestPart]
    pull.state = "open"
    part.ref, part.sha = "feature/x", "abc"
    part.repo = gh[Repository]
    pull.head = pull.base = part
    gh[Repository].full_name = "me/app"
    state = writer().pull_state("me/app", 7)
    assert (state.open, state.head_ref, state.head_sha, state.from_fork) == (True, "feature/x", "abc", False)

    gh[Repository].get_collaborator_permission.side_effect = None
    gh[Repository].get_collaborator_permission.return_value = "write"
    assert writer().permission("me/app", "maintainer") == "write"
    gh[Repository].get_collaborator_permission.side_effect = GithubException(404, {}, None)
    assert writer().permission("me/app", "ghost") == "none"


def test_branches_and_commits(gh: Mocks) -> None:
    w = writer()
    w.advance_branch("me/app", "feature/x", "def")
    gh[Repository].get_git_ref.assert_called_with("heads/feature/x")
    gh[GitRef].edit.assert_called_with("def", force=False)

    w.set_branch("me/app", "driftless/pr-7", "def")
    gh[GitRef].edit.assert_called_with("def", force=True)
    gh[Repository].get_git_ref.side_effect = GithubException(404, {}, None)
    w.set_branch("me/app", "driftless/pr-8", "def")
    gh[Repository].create_git_ref.assert_called_once_with("refs/heads/driftless/pr-8", "def")

    gh[GitCommit].sha = "new"
    assert w.commit_files("me/app", "parent", {"README.md": "x"}, "docs: sync") == "new"
    gh[Repository].create_git_tree.assert_called_once()
    gh[Repository].create_git_commit.assert_called_once()


def test_default_branch_and_pulls(gh: Mocks) -> None:
    repo = gh[Repository]
    repo.default_branch = "main"
    gh[Branch].commit = gh[Commit]
    gh[Commit].sha = "head"
    assert writer().default_branch("me/app") == ("main", "head")

    gh[PullRequest].number, gh[PullRequest].html_url = 9, "https://github.com/me/app/pull/9"
    assert writer().create_pull("me/app", "driftless/pr-7", "main", "t", "b").number == 9
    assert writer().update_pull("me/app", 9, "t", "b").number == 9
    gh[PullRequest].edit.assert_called_once_with(title="t", body="b")

    repo.owner = gh[NamedUser]
    gh[NamedUser].login = "me"
    repo.get_pulls.side_effect = lambda **kwargs: [gh[PullRequest]]
    assert writer().find_open_pull("me/app", "driftless/pr-7").number == 9


def test_a_method_missing_from_pygithub_fails_loudly(gh: Mocks) -> None:
    with pytest.raises(AttributeError):
        gh[Repository].get_issue_comment(5)  # what the old edit_comment called
