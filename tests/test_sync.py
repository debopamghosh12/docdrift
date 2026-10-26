from collections.abc import Callable
from typing import Any

import pytest

import driftless.github_writer
import driftless.sync
from driftless.config import Settings
from driftless.models import DocFile, SourceFile
from driftless.pipeline import AnalysisResult
from driftless.pr_writer import PublishAction
from driftless.premerge import SupersededError
from driftless.proposals import DocProposals
from driftless.sync import GitHubSyncer
from tests.test_premerge import HEAD, FakeCheckRepo, open_pr
from tests.test_pr_writer import REPO, proposals


class FakeAnalysis:
    """Stands in for analyze_and_propose: the PR's head is whatever FakeCheckRepo's branch says."""

    def __init__(self, gh: FakeCheckRepo, result: Callable[[str], DocProposals]) -> None:
        self.gh = gh
        self.result = result
        self.runs: list[str] = []

    def __call__(
        self, settings: Settings, client: Any, llm: Any, repo: str, number: int, *args: Any,
        pre_merge: bool = False, checkpoint: Callable[[AnalysisResult], None] = lambda r: None, **kwargs: Any,
    ) -> tuple[AnalysisResult, DocProposals]:
        head = self.gh.branches["feature/x"]
        self.runs.append(head)
        analysis = open_pr(head)
        checkpoint(analysis)
        return analysis, self.result(head)


class FakeBaseClient:
    """The PR's merge base: no docs or sources unless a test sets them."""

    def __init__(self) -> None:
        self.docs: list[DocFile] = []
        self.sources: list[SourceFile] = []
        self.refs: list[str] = []

    def fetch_docs(self, repo: str, ref: str) -> list[DocFile]:
        self.refs.append(ref)
        return self.docs

    def fetch_sources(self, repo: str, ref: str) -> list[SourceFile]:
        return self.sources

    def fetch_files(self, repo: str, ref: str) -> dict[str, str]:
        return {}


@pytest.fixture
def base() -> FakeBaseClient:
    return FakeBaseClient()


@pytest.fixture
def gh(monkeypatch: pytest.MonkeyPatch, base: FakeBaseClient) -> FakeCheckRepo:
    fake = FakeCheckRepo()
    monkeypatch.setattr(driftless.github_writer, "GitHubWriter", lambda token: fake)
    monkeypatch.setattr(driftless.sync, "_clients", lambda settings: (base, None))
    return fake


def syncer() -> GitHubSyncer:
    return GitHubSyncer(Settings(_env_file=None, github_token="t", llm_api_key="k", execute_examples=False))


def test_pre_merge_sets_pending_then_the_result(gh: FakeCheckRepo, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(driftless.sync, "analyze_and_propose", FakeAnalysis(gh, lambda head: proposals()))
    published = syncer().pre_merge(REPO, 7, HEAD, lambda: False)
    assert [s[:2] for s in gh.statuses] == [(HEAD, "pending"), (HEAD, "failure")]
    assert published.check_state == "failure"


def test_superseded_check_neither_comments_nor_sets_a_final_status(
    gh: FakeCheckRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(driftless.sync, "analyze_and_propose", FakeAnalysis(gh, lambda head: proposals()))
    with pytest.raises(SupersededError):
        syncer().pre_merge(REPO, 7, HEAD, lambda: True)
    with pytest.raises(SupersededError):  # the PR head already moved past the pushed commit
        syncer().pre_merge(REPO, 7, "f" * 40, lambda: False)
    assert gh.comments == {}
    assert all(state == "pending" for _, state, _, _ in gh.statuses)


def test_a_crash_sets_an_error_status(gh: FakeCheckRepo, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("tarball download failed")

    monkeypatch.setattr(driftless.sync, "analyze_and_propose", boom)
    with pytest.raises(RuntimeError):
        syncer().pre_merge(REPO, 7, HEAD, lambda: False)
    assert gh.statuses[-1][1:3] == ("error", "Driftless failed: RuntimeError")


def test_apply_reuses_the_last_check_then_the_next_push_is_in_sync(
    gh: FakeCheckRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Stale at the original head; in sync once Driftless's commit is the head.
    analysis = FakeAnalysis(gh, lambda head: proposals() if head == HEAD else DocProposals())
    monkeypatch.setattr(driftless.sync, "analyze_and_propose", analysis)
    s = syncer()
    s.pre_merge(REPO, 7, HEAD, lambda: False)

    applied = s.apply(REPO, 7, "maintainer")
    assert applied.action == PublishAction.COMMITTED
    assert analysis.runs == [HEAD]  # the comment's fixes were reused, no second LLM run
    new_head = gh.branches["feature/x"]
    assert new_head != HEAD

    s.pre_merge(REPO, 7, new_head, lambda: False)  # the synchronize event for Driftless's push
    assert gh.statuses[-1][:2] == (new_head, "success")
    assert "✅ Docs are in sync" in gh.comments[1].body


def test_apply_rechecks_when_the_head_moved_since_the_comment(
    gh: FakeCheckRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    analysis = FakeAnalysis(gh, lambda head: proposals())
    monkeypatch.setattr(driftless.sync, "analyze_and_propose", analysis)
    s = syncer()
    s.pre_merge(REPO, 7, HEAD, lambda: False)
    gh.branches["feature/x"] = "e" * 40  # someone pushed; the cached fixes are for an old head

    s.apply(REPO, 7, "maintainer")
    [(_, (parent, _))] = gh.commits.items()
    assert analysis.runs == [HEAD, "e" * 40]
    assert parent == "e" * 40


def test_pre_merge_reads_the_merge_base_for_pre_existing_drift(
    gh: FakeCheckRepo, base: FakeBaseClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(driftless.sync, "analyze_and_propose", FakeAnalysis(gh, lambda head: proposals()))
    syncer().pre_merge(REPO, 7, HEAD, lambda: False)
    assert base.refs == ["a" * 40]  # the PR's merge base
