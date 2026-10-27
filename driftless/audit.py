"""Docs health audit (`driftless check` and the web app's "Run check"): run every example in a
repo's docs at a ref against its app; with `fix`, repair broken examples and open one PR."""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from driftless.config import Settings
from driftless.doc_editor import unified_diff
from driftless.execution import ExecutionReport
from driftless.pr_writer import PublishResult
from driftless.progress import Stage, StageProgress, silent
from driftless.sandbox import SandboxLimits

if TYPE_CHECKING:
    from driftless.example_fixer import ExampleFixer


class AuditConfigError(RuntimeError):
    """A setting the audit needs is missing (token or LLM key for --fix)."""


@dataclass
class AuditResult:
    repo: str
    ref: str
    report: ExecutionReport
    originals: dict[str, str]  # doc text at `ref`, by path
    fix: bool = False
    published: PublishResult | None = None
    # Why fixing opened no PR: nothing was fixed, or every fixed file moved on the default branch.
    publish_note: str | None = None

    @property
    def diffs(self) -> dict[str, str]:
        """Unified diff per doc file the fixer changed."""
        return {
            path: unified_diff(path, self.originals[path], text)
            for path, text in self.report.contents.items()
            if path in self.originals and text != self.originals[path]
        }

    @property
    def summary(self) -> str:
        report = self.report
        if report.errors and not report.outcomes:
            return "could not run the examples"
        return f"{report.passing}/{report.total} examples passing"


def run_audit(
    settings: Settings, repo: str, ref: str, fix: bool = False, progress: StageProgress = silent
) -> AuditResult:
    """Raises AuditConfigError when `fix` lacks a token or an LLM; GitHub/HTTP errors propagate."""
    from driftless.executable_docs import execute_docs
    from driftless.github_client import GitHubClient

    fixer = _fixer(settings) if fix else None
    client = GitHubClient(settings.github_token or None)
    progress(Stage.FETCH, f"Downloading {repo}@{ref}...")
    docs = client.fetch_docs(repo, ref)
    files = client.fetch_files(repo, ref)
    originals = {d.path: d.content for d in docs}
    progress(Stage.START, "Installing the app's dependencies (cached after the first run) and starting it...")
    report = execute_docs(
        files,
        originals,
        fixer,
        SandboxLimits.from_settings(settings),
        lambda message: progress(Stage.RUN, message),
    )
    audit = AuditResult(repo=repo, ref=ref, report=report, originals=originals, fix=fix)
    if fix:
        _publish(settings, audit, progress)
    return audit


def _fixer(settings: Settings) -> "ExampleFixer":
    from driftless.example_fixer import ExampleFixer
    from driftless.llm import JsonLLM, LLMError

    if not settings.github_token:
        raise AuditConfigError("GITHUB_TOKEN is required to open a PR with the fixed examples.")
    try:
        return ExampleFixer(JsonLLM.from_settings(settings))
    except LLMError as exc:
        raise AuditConfigError(str(exc)) from exc


def _publish(settings: Settings, audit: AuditResult, progress: StageProgress) -> None:
    from driftless.github_writer import GitHubWriter
    from driftless.pr_writer import PRWriter

    if not audit.report.fixed:
        audit.publish_note = "No examples were fixed, so no PR was opened."
        return
    progress(Stage.PUBLISH, "Opening the examples PR...")
    writer = PRWriter(GitHubWriter(settings.github_token))
    audit.published = writer.publish_example_fixes(audit.repo, audit.ref, audit.originals, audit.report)
    if audit.published is None:
        audit.publish_note = f"The fixed files changed on the default branch since {audit.ref}; no PR opened."
