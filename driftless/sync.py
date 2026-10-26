"""Headless runs used by the server: post-merge sync, pre-merge check, `/driftless apply`,
memory commands, and the web app's dry-run analysis and docs health audit.

Every run uses what Driftless remembers for the repo (ignore rules, style notes, rejected
changes), and every docs PR it opens is remembered so its fate can be fed back. The CLI runs the
same steps with rich output in between.
"""

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from driftless.config import Settings
from driftless.memory import Memory, RepoMemory
from driftless.pr_writer import PublishAction, PublishResult
from driftless.premerge import PendingFixes, PullState, SupersededError
from driftless.progress import Stage, StageProgress, silent

if TYPE_CHECKING:
    from driftless.audit import AuditResult
    from driftless.github_client import GitHubClient
    from driftless.llm import JsonLLM
    from driftless.models import DocFile
    from driftless.pipeline import AnalysisResult
    from driftless.proposals import DocProposals


# A dry-run analysis is reused by the next publish of the same PR for this long.
ANALYSIS_TTL_S = 30 * 60


class SyncConfigError(RuntimeError):
    pass


@dataclass
class PullAnalysis:
    """A dry run (the web app's "Analyze"): what a pre-merge check or post-merge sync would publish."""

    result: "AnalysisResult"
    proposals: "DocProposals"
    min_confidence: float
    pre_merge: bool  # analyzed at the open PR's head (else at the merge commit)
    created_at: float
    published: PublishResult | None = None  # dry runs publish nothing

    @property
    def summary(self) -> str:
        from driftless.doc_editor import final_docs

        changed = final_docs(self.result.docs, self.proposals, self.min_confidence)
        if not changed:
            return "docs in sync"
        return f"{len(changed)} doc file{'s' if len(changed) != 1 else ''} to change"


def sync_pull_request(
    settings: Settings,
    repo: str,
    number: int,
    min_confidence: float = 0.5,
    top_k: int = 10,
    memory: Memory | None = None,
) -> PublishResult:
    """Raises OwnPullRequestError for Driftless's own PRs and SyncConfigError for missing secrets."""
    client, llm = _clients(settings)
    repo_memory = memory.repo_memory(repo) if memory else None
    result, proposals = analyze_and_propose(
        settings, client, llm, repo, number, min_confidence, top_k, repo_memory=repo_memory
    )
    return publish_docs_pr(settings, result, proposals, min_confidence, memory)


def publish_docs_pr(
    settings: Settings,
    result: "AnalysisResult",
    proposals: "DocProposals",
    min_confidence: float,
    memory: Memory | None = None,
) -> PublishResult:
    """Open/update the docs PR (or comment "in sync" on the source PR) and remember what it proposes."""
    from driftless.github_writer import GitHubWriter
    from driftless.pr_writer import PRWriter

    published = PRWriter(GitHubWriter(settings.github_token)).publish(result, proposals, min_confidence)
    if memory:
        remember_docs_pr(memory, result, proposals, min_confidence, published)
    return published


def remember_docs_pr(
    memory: Memory, result: "AnalysisResult", proposals: "DocProposals", min_confidence: float, published: PublishResult
) -> None:
    """Keep what an opened/updated docs PR proposes, to learn from it once it's merged or closed."""
    from driftless.feedback import proposed_changes

    if published.action in (PublishAction.OPENED, PublishAction.UPDATED) and published.number:
        changes = proposed_changes(result, proposals, min_confidence, published.files)
        memory.record_docs_pr(result.pr.repo, published.number, result.pr.number, changes)


def analyze_and_propose(
    settings: Settings,
    client: "GitHubClient",
    llm: "JsonLLM",
    repo: str,
    number: int,
    min_confidence: float = 0.5,
    top_k: int = 10,
    pre_merge: bool = False,
    checkpoint: Callable[["AnalysisResult"], None] = lambda result: None,
    repo_memory: RepoMemory | None = None,
    progress: StageProgress = silent,
) -> tuple["AnalysisResult", "DocProposals"]:
    """Analyze -> LLM rewrites + verification -> executed examples. `checkpoint` runs between
    stages (pre-merge checks raise SupersededError there once a newer push arrived)."""
    from pathlib import Path

    from driftless.analyzer import Analyzer
    from driftless.pipeline import analyze_pull_request

    progress(Stage.FETCH, f"Reading {repo}#{number}, its docs and code...")
    result = analyze_pull_request(client, repo, number, top_k=top_k, pre_merge=pre_merge, memory=repo_memory)
    checkpoint(result)
    progress(
        Stage.REVIEW,
        f"Reviewing {len(result.candidates)} candidate sections and {len(result.missing)} missing items with the model...",
    )
    proposals = Analyzer(llm).propose(result, min_confidence=min_confidence)
    checkpoint(result)
    if settings.execute_examples:
        from driftless.example_fixer import ExampleFixer
        from driftless.executable_docs import execute_all_docs

        progress(Stage.START, "Installing the app's dependencies (cached after the first run) and starting it...")
        proposals.execution = execute_all_docs(
            executable_docs(result),
            client.fetch_files(repo, result.snapshot_ref),
            proposals,
            min_confidence,
            ExampleFixer(llm),
            Path(settings.sandbox_cache_dir),
            settings.sandbox_startup_timeout,
            progress=lambda message: progress(Stage.RUN, message),
        )
        checkpoint(result)
    return result, proposals


def mark_baseline(
    settings: Settings, client: "GitHubClient", result: "AnalysisResult", proposals: "DocProposals"
) -> None:
    """Pre-merge: flag drift that already existed at the PR's merge base (see baseline.py).

    Runs the base's docs against the base's app (no fixer) only when the head had failing or
    fixed examples, and indexes the base's code only when there are stale sections to classify.
    """
    from pathlib import Path

    from driftless.baseline import mark_pre_existing, needs_baseline
    from driftless.code_index import build_code_index
    from driftless.executable_docs import execute_docs

    examples, sections = needs_baseline(proposals)
    if not (examples or sections):
        return
    repo, base = result.pr.repo, result.pr.merge_base_sha
    base_docs = client.fetch_docs(repo, base)
    base_execution = None
    if examples and settings.execute_examples:
        base_execution = execute_docs(
            client.fetch_files(repo, base),
            {d.path: d.content for d in base_docs if result.memory.ignoring_file_rule(d.path) is None},
            None,
            Path(settings.sandbox_cache_dir),
            settings.sandbox_startup_timeout,
        )
    base_code = build_code_index(client.fetch_sources(repo, base)) if sections else None
    mark_pre_existing(proposals, result.code, base_docs, base_code, base_execution)


def executable_docs(result: "AnalysisResult") -> list["DocFile"]:
    """The doc files whose examples run: all of them except those an ignore rule matches.
    Such a rule counts as applied (for the 🧠 line) when the file has examples."""
    from driftless.examples import extract_examples

    kept = []
    for doc in result.docs:
        rule = result.memory.ignoring_file_rule(doc.path)
        if rule is None:
            kept.append(doc)
        elif rule not in result.ignored and extract_examples(doc.path, doc.content):
            result.ignored.append(rule)
    return kept


class GitHubSyncer:
    """Runs each webhook job mode against GitHub and the LLM.

    Remembers the verified fixes of the last pre-merge check per PR, so `/driftless apply`
    commits exactly what the comment showed (when the head hasn't moved) without a second LLM run.
    In another process (a GitHub Actions runner, a restarted server) they are read back from the
    commit the comment points at.
    Likewise the web app's last dry run per PR is published as-is by the next pre-merge check or
    post-merge sync of that PR, when it is recent, at the same commit and with the same memory.
    """

    def __init__(
        self, settings: Settings, min_confidence: float = 0.5, top_k: int = 10, memory: Memory | None = None
    ) -> None:
        self._settings = settings
        self._min_confidence = min_confidence
        self._top_k = top_k
        self._memory = memory or Memory(settings.database_url)
        self._fixes: dict[tuple[str, int], PendingFixes] = {}
        self._analyses: dict[tuple[str, int], PullAnalysis] = {}
        self._lock = threading.Lock()

    def post_merge(self, repo: str, number: int) -> PublishResult:
        cached = self._take_analysis(repo, number, pre_merge=False)
        if cached is None:
            return sync_pull_request(self._settings, repo, number, self._min_confidence, self._top_k, self._memory)
        return publish_docs_pr(self._settings, cached.result, cached.proposals, self._min_confidence, self._memory)

    def analyze(self, repo: str, number: int, progress: StageProgress = silent) -> PullAnalysis:
        """Dry run: everything a pre-merge check (open PR) or post-merge sync (merged PR) does,
        without publishing. Kept for the next publish of this PR."""
        from driftless.github_writer import GitHubWriter

        client, llm = _clients(self._settings)
        pre_merge = GitHubWriter(self._settings.github_token).pull_state(repo, number).open
        result, proposals = analyze_and_propose(
            self._settings, client, llm, repo, number, self._min_confidence, self._top_k,
            pre_merge=pre_merge, repo_memory=self._memory.repo_memory(repo), progress=progress,
        )
        if pre_merge:
            progress(Stage.BASELINE, "Checking which drift already existed at the PR's base commit...")
            mark_baseline(self._settings, client, result, proposals)
        analysis = PullAnalysis(result, proposals, self._min_confidence, pre_merge, time.time())
        with self._lock:
            self._analyses[(repo.lower(), number)] = analysis
        return analysis

    def check(self, repo: str, ref: str, fix: bool = False, progress: StageProgress = silent) -> "AuditResult":
        """Docs health audit (`driftless check [--fix]`)."""
        from driftless.audit import AuditConfigError, run_audit

        try:
            return run_audit(self._settings, repo, ref, fix, progress)
        except AuditConfigError as exc:
            raise SyncConfigError(str(exc)) from exc

    def _take_analysis(
        self, repo: str, number: int, pre_merge: bool, head_sha: str | None = None
    ) -> PullAnalysis | None:
        """The PR's dry run, if it is what this publish would compute anyway (each is used once)."""
        with self._lock:
            cached = self._analyses.pop((repo.lower(), number), None)
        if cached is None or cached.pre_merge != pre_merge or time.time() - cached.created_at > ANALYSIS_TTL_S:
            return None
        if head_sha is not None and cached.result.pr.head_sha != head_sha:
            return None
        if cached.result.memory != self._memory.repo_memory(repo):
            return None  # an ignore rule or style note changed since
        return cached

    def remember(self, repo: str, number: int, user: str, command: str) -> PublishResult:
        from driftless.commands import MemoryCommands, parse_command
        from driftless.github_writer import GitHubWriter

        parsed = parse_command(command)
        if parsed is None:
            raise ValueError(f"not a memory command: {command!r}")
        if not self._settings.github_token:
            raise SyncConfigError("GITHUB_TOKEN is required to reply to commands")
        return MemoryCommands(GitHubWriter(self._settings.github_token), self._memory).run(repo, number, user, parsed)

    def pre_merge(self, repo: str, number: int, head_sha: str, should_stop: Callable[[], bool]) -> PublishResult:
        from driftless.github_writer import GitHubWriter
        from driftless.premerge import PreMergeChecker

        client, llm = _clients(self._settings)
        checker = PreMergeChecker(GitHubWriter(self._settings.github_token))
        checker.start(repo, head_sha)

        def checkpoint(result: "AnalysisResult") -> None:
            if should_stop():
                raise SupersededError(f"a newer run replaced the check of {head_sha[:7]}")
            if result.pr.head_sha != head_sha:
                raise SupersededError(f"{repo}#{number} moved from {head_sha[:7]} to {result.pr.head_sha[:7]}")

        try:
            cached = self._take_analysis(repo, number, pre_merge=True, head_sha=head_sha)
            if cached is not None:
                result, proposals = cached.result, cached.proposals
            else:
                result, proposals = analyze_and_propose(
                    self._settings, client, llm, repo, number, self._min_confidence, self._top_k,
                    pre_merge=True, checkpoint=checkpoint, repo_memory=self._memory.repo_memory(repo),
                )
                mark_baseline(self._settings, client, result, proposals)
            checkpoint(result)
            published, fixes = checker.report(result, proposals, self._min_confidence)
        except SupersededError:
            raise  # the newer run owns the comment and the status
        except Exception as exc:
            checker.fail(repo, head_sha, exc)
            raise
        with self._lock:
            self._fixes[(repo.lower(), number)] = fixes
        return published

    def apply(self, repo: str, number: int, user: str) -> PublishResult:
        from driftless.github_writer import GitHubWriter
        from driftless.premerge import PreMergeChecker

        key = (repo.lower(), number)
        checker = PreMergeChecker(GitHubWriter(self._settings.github_token))

        def fixes_for(pull: PullState) -> PendingFixes:
            with self._lock:
                cached = self._fixes.get(key)
            if cached is not None and cached.head_sha == pull.head_sha:
                return cached
            # What the comment showed, from its fixes commit (another process ran the check), else re-check.
            stored = checker.stored_fixes(repo, number, pull.head_sha)
            if stored is not None:
                return stored
            self.pre_merge(repo, number, pull.head_sha, lambda: False)  # also refreshes the comment
            with self._lock:
                return self._fixes[key]

        return checker.apply(repo, number, user, fixes_for)


def _clients(settings: Settings) -> tuple["GitHubClient", "JsonLLM"]:
    from driftless.github_client import GitHubClient
    from driftless.llm import JsonLLM, LLMError

    if not settings.github_token:
        raise SyncConfigError("GITHUB_TOKEN is required to comment on and open PRs")
    try:
        llm = JsonLLM.from_settings(settings)
    except LLMError as exc:
        raise SyncConfigError(str(exc)) from exc
    return GitHubClient(settings.github_token), llm
