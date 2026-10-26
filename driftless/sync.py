"""Headless end-to-end sync of one merged PR: analyze, propose + verify, publish.

The CLI runs the same steps with rich output in between; the webhook server calls this.
"""

from driftless.config import Settings
from driftless.pr_writer import PublishResult


class SyncConfigError(RuntimeError):
    pass


def sync_pull_request(
    settings: Settings, repo: str, number: int, min_confidence: float = 0.5, top_k: int = 10
) -> PublishResult:
    """Raises OwnPullRequestError for Driftless's own PRs and SyncConfigError for missing secrets."""
    from driftless.analyzer import Analyzer
    from driftless.github_client import GitHubClient
    from driftless.github_writer import GitHubWriter
    from driftless.llm import JsonLLM, LLMError
    from driftless.pipeline import analyze_pull_request
    from driftless.pr_writer import PRWriter

    if not settings.github_token:
        raise SyncConfigError("GITHUB_TOKEN is required to open a docs PR")
    try:
        llm = JsonLLM.from_settings(settings)
    except LLMError as exc:
        raise SyncConfigError(str(exc)) from exc

    client = GitHubClient(settings.github_token)
    result = analyze_pull_request(client, repo, number, top_k=top_k)
    proposals = Analyzer(llm).propose(result, min_confidence=min_confidence)
    if settings.execute_examples:
        from pathlib import Path

        from driftless.example_fixer import ExampleFixer
        from driftless.executable_docs import execute_updated_docs

        proposals.execution = execute_updated_docs(
            result.docs,
            client.fetch_files(repo, result.pr.merge_commit_sha),
            proposals,
            min_confidence,
            ExampleFixer(llm),
            Path(settings.sandbox_cache_dir),
            settings.sandbox_startup_timeout,
        )
    return PRWriter(GitHubWriter(settings.github_token)).publish(result, proposals, min_confidence)
