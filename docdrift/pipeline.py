"""Orchestrate the end-to-end DocDrift run for a merged PR."""

from typing import Protocol

from pydantic import BaseModel

from docdrift.candidates import find_missing_docs, select_candidates
from docdrift.code_index import CodeIndex, build_code_index
from docdrift.diff_parser import build_changeset, is_source_path
from docdrift.doc_index import index_documents
from docdrift.models import (
    Candidate,
    ChangeSet,
    DocFile,
    DocSection,
    MissingDoc,
    PullRequestInfo,
    SourceFile,
)


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
    docs = provider.fetch_docs(repo_name, pr.merge_commit_sha)
    sources = provider.fetch_sources(repo_name, pr.merge_commit_sha)
    return analyze(pr, docs, top_k=top_k, sources=sources)
