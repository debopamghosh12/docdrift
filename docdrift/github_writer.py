"""GitHub write operations for the docs PR (PyGithub implementation of pr_writer.DocsRepo)."""

from github import Auth, Github, GithubException, InputGitTreeElement
from github.Repository import Repository

from docdrift.github_client import read_text
from docdrift.pr_writer import CommentRef, PullRequestRef


class GitHubWriter:
    def __init__(self, token: str) -> None:
        self._gh = Github(auth=Auth.Token(token))
        self._repos: dict[str, Repository] = {}

    def default_branch(self, repo: str) -> tuple[str, str]:
        r = self._repo(repo)
        return r.default_branch, r.get_branch(r.default_branch).commit.sha

    def read_file(self, repo: str, path: str, ref: str) -> str | None:
        return read_text(self._repo(repo), path, ref)

    def commit_files(self, repo: str, parent_sha: str, files: dict[str, str], message: str) -> str:
        # One commit via the Git Data API: a tree on top of the parent's tree, then the commit.
        r = self._repo(repo)
        parent = r.get_git_commit(parent_sha)
        elements = [InputGitTreeElement(path, "100644", "blob", content=text) for path, text in files.items()]
        tree = r.create_git_tree(elements, base_tree=parent.tree)
        return r.create_git_commit(message, tree, [parent]).sha

    def set_branch(self, repo: str, branch: str, sha: str) -> None:
        r = self._repo(repo)
        try:
            ref = r.get_git_ref(f"heads/{branch}")
        except GithubException as exc:
            if exc.status != 404:
                raise
            r.create_git_ref(f"refs/heads/{branch}", sha)
            return
        ref.edit(sha, force=True)

    def find_open_pull(self, repo: str, head_branch: str) -> PullRequestRef | None:
        r = self._repo(repo)
        owner = r.owner.login
        for pull in r.get_pulls(state="open", head=f"{owner}:{head_branch}"):
            return PullRequestRef(number=pull.number, url=pull.html_url)
        return None

    def create_pull(self, repo: str, head: str, base: str, title: str, body: str) -> PullRequestRef:
        pull = self._repo(repo).create_pull(base=base, head=head, title=title, body=body)
        return PullRequestRef(number=pull.number, url=pull.html_url)

    def update_pull(self, repo: str, number: int, title: str, body: str) -> PullRequestRef:
        pull = self._repo(repo).get_pull(number)
        pull.edit(title=title, body=body)
        return PullRequestRef(number=pull.number, url=pull.html_url)

    def list_comments(self, repo: str, number: int) -> list[CommentRef]:
        issue = self._repo(repo).get_issue(number)
        return [CommentRef(id=c.id, body=c.body or "", url=c.html_url) for c in issue.get_comments()]

    def create_comment(self, repo: str, number: int, body: str) -> str:
        return self._repo(repo).get_issue(number).create_comment(body).html_url

    def edit_comment(self, repo: str, comment_id: int, body: str) -> str:
        comment = self._repo(repo).get_issue_comment(comment_id)
        comment.edit(body)
        return comment.html_url

    def _repo(self, name: str) -> Repository:
        if name not in self._repos:
            self._repos[name] = self._gh.get_repo(name)
        return self._repos[name]
