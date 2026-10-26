"""Orchestrate the end-to-end Driftless run for a merged PR."""

from typing import Protocol

from pydantic import BaseModel

from driftless.candidates import find_missing_docs, select_candidates
from driftless.code_index import CodeIndex, build_code_index
from driftless.diff_parser import build_changeset, is_source_path
from driftless.doc_index import index_documents
from driftless.models import (
    Candidate,
    ChangeSet,
    DocFile,
    DocSection,
    MissingDoc,
    PullRequestInfo,
    SourceFile,
)


# Branch prefix Driftless pushes its own docs PRs from. Those, and branches from before the
# rename (DocDrift), are never analyzed: it would loop.
DRIFTLESS_BRANCH_PREFIX = "driftless/"
OWN_BRANCH_PREFIXES = (DRIFTLESS_BRANCH_PREFIX, "docdrift/")


class OwnPullRequestError(RuntimeError):
    pass


def is_own_branch(ref: str) -> bool:
    return ref.startswith(OWN_BRANCH_PREFIXES)


def is_driftless_pull_request(pr: PullRequestInfo) -> bool:
    return is_own_branch(pr.head_ref)


class SourceProvider(Protocol):
    def fetch_pull_request(self, repo_name: str, number: int) -> PullRequestInfo: ...

    def fetch_docs(self, repo_name: str, ref: str) -> list[DocFile]: ...

    def fetch_sources(self, repo_name: str, ref: str) -> list[SourceFile]: ...


class AnalysisResult(BaseModel):
    pr: PullRequestInfo
    changeset: ChangeSet
    docs: list[DocFile]
    sections: list[DocSection]
    candidates: list[Candidate]
    missing: list[MissingDoc]
    code: CodeIndex  # facts at the merge commit, used by the verifier


def analyze(
    pr: PullRequestInfo,
    docs: list[DocFile],
    top_k: int = 10,
    sources: list[SourceFile] | None = None,
) -> AnalysisResult:
    """`sources` defaults to the PR's changed files when a full snapshot isn't available."""
    if sources is None:
        sources = [
            SourceFile(path=f.path, content=f.after)
            for f in pr.files
            if f.after is not None and is_source_path(f.path)
        ]
    changeset = build_changeset(pr.files)
    sections = index_documents(docs)
    return AnalysisResult(
        pr=pr,
        changeset=changeset,
        docs=docs,
        sections=sections,
        candidates=select_candidates(sections, changeset, top_k=top_k),
        missing=find_missing_docs(sections, changeset),
        code=build_code_index(sources),
    )


def analyze_pull_request(
    provider: SourceProvider, repo_name: str, number: int, top_k: int = 10
) -> AnalysisResult:
    pr = provider.fetch_pull_request(repo_name, number)
    if is_driftless_pull_request(pr):
        raise OwnPullRequestError(f"{repo_name}#{number} is a Driftless docs PR ({pr.head_ref}); skipping")
    docs = provider.fetch_docs(repo_name, pr.merge_commit_sha)
    sources = provider.fetch_sources(repo_name, pr.merge_commit_sha)
    return analyze(pr, docs, top_k=top_k, sources=sources)
