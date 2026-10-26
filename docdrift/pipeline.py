"""Orchestrate the end-to-end DocDrift run for a merged PR."""

from typing import Protocol

from pydantic import BaseModel

from docdrift.candidates import find_missing_docs, select_candidates
from docdrift.diff_parser import build_changeset
from docdrift.doc_index import index_documents
from docdrift.models import Candidate, ChangeSet, DocFile, DocSection, MissingDoc, PullRequestInfo


class SourceProvider(Protocol):
    def fetch_pull_request(self, repo_name: str, number: int) -> PullRequestInfo: ...

    def fetch_docs(self, repo_name: str, ref: str) -> list[DocFile]: ...


class AnalysisResult(BaseModel):
    pr: PullRequestInfo
    changeset: ChangeSet
    docs: list[DocFile]
    sections: list[DocSection]
    candidates: list[Candidate]
    missing: list[MissingDoc]


def analyze(pr: PullRequestInfo, docs: list[DocFile], top_k: int = 10) -> AnalysisResult:
    changeset = build_changeset(pr.files)
    sections = index_documents(docs)
    return AnalysisResult(
        pr=pr,
        changeset=changeset,
        docs=docs,
        sections=sections,
        candidates=select_candidates(sections, changeset, top_k=top_k),
        missing=find_missing_docs(sections, changeset),
    )


def analyze_pull_request(
    provider: SourceProvider, repo_name: str, number: int, top_k: int = 10
) -> AnalysisResult:
    pr = provider.fetch_pull_request(repo_name, number)
    docs = provider.fetch_docs(repo_name, pr.merge_commit_sha)
    return analyze(pr, docs, top_k=top_k)
