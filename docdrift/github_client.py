"""GitHub API wrapper: fetch merged PR diffs, file contents, docs and source snapshots."""

from github import Auth, Github, GithubException
from github.File import File
from github.Repository import Repository

from docdrift.diff_parser import is_source_path, is_test_path
from docdrift.doc_index import is_doc_path
from docdrift.models import ChangedFile, DocFile, FileStatus, PullRequestInfo, SourceFile
from docdrift.snapshot import download_snapshot

_STATUS_MAP = {
    "added": FileStatus.ADDED,
    "copied": FileStatus.ADDED,
    "removed": FileStatus.REMOVED,
    "renamed": FileStatus.RENAMED,
}


class PullRequestNotMergedError(RuntimeError):
    pass


class GitHubClient:
    def __init__(self, token: str | None = None, max_docs: int = 200) -> None:
        self._gh = Github(auth=Auth.Token(token)) if token else Github()
        self._token = token
        self._max_docs = max_docs
        self._repos: dict[str, Repository] = {}
        self._snapshots: dict[tuple[str, str], dict[str, str]] = {}

    def fetch_pull_request(self, repo_name: str, number: int) -> PullRequestInfo:
        """Fetch a merged PR with patches and full before/after content for source files.

        "Before" is the merge base and "after" is the PR head, which is exactly what the
        PR's patches describe, so unrelated commits on the base branch don't leak in.
        """
        repo = self._repo(repo_name)
        pr = repo.get_pull(number)
        if not pr.merged:
            raise PullRequestNotMergedError(f"{repo_name}#{number} is not merged")
        merge_base = repo.compare(pr.base.sha, pr.head.sha).merge_base_commit.sha
        files = [self._changed_file(repo, f, merge_base, pr.head.sha) for f in pr.get_files()]
        return PullRequestInfo(
            repo=repo_name,
            number=number,
            title=pr.title,
            body=pr.body or "",
            author=pr.user.login if pr.user else "",
            url=pr.html_url,
            base_ref=pr.base.ref,
            merge_base_sha=merge_base,
            head_sha=pr.head.sha,
            merge_commit_sha=pr.merge_commit_sha,
            files=files,
        )

    def fetch_docs(self, repo_name: str, ref: str) -> list[DocFile]:
        """Return README.md and docs/**/*.md at `ref`."""
        files = self._snapshot(repo_name, ref)
        docs = [DocFile(path=p, content=c) for p, c in sorted(files.items()) if is_doc_path(p)]
        return docs[: self._max_docs]

    def fetch_sources(self, repo_name: str, ref: str) -> list[SourceFile]:
        """Return non-test source files at `ref`, used to verify generated docs."""
        files = self._snapshot(repo_name, ref)
        return [
            SourceFile(path=p, content=c)
            for p, c in sorted(files.items())
            if is_source_path(p) and not is_test_path(p)
        ]

    def _snapshot(self, repo_name: str, ref: str) -> dict[str, str]:
        key = (repo_name, ref)
        if key not in self._snapshots:
            self._snapshots[key] = download_snapshot(repo_name, ref, self._token)
        return self._snapshots[key]

    def _repo(self, name: str) -> Repository:
        if name not in self._repos:
            self._repos[name] = self._gh.get_repo(name)
        return self._repos[name]

    def _changed_file(self, repo: Repository, file: File, before_ref: str, after_ref: str) -> ChangedFile:
        status = _STATUS_MAP.get(file.status, FileStatus.MODIFIED)
        previous = file.previous_filename if status == FileStatus.RENAMED else None
        before = after = None
        if is_source_path(file.filename):
            if status != FileStatus.ADDED:
                before = _read_text(repo, previous or file.filename, before_ref)
            if status != FileStatus.REMOVED:
                after = _read_text(repo, file.filename, after_ref)
        return ChangedFile(
            path=file.filename,
            status=status,
            previous_path=previous,
            patch=file.patch or "",
            before=before,
            after=after,
        )


def _read_text(repo: Repository, path: str, ref: str) -> str | None:
    try:
        content = repo.get_contents(path, ref=ref)
        if isinstance(content, list):
            return None
        return content.decoded_content.decode("utf-8")
    except (GithubException, AssertionError, UnicodeDecodeError):
        # Too large, binary, or missing: the diff parser falls back to the patch.
        return None
