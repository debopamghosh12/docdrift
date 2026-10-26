"""Headless end-to-end sync of one merged PR: analyze, propose + verify, publish.

The CLI runs the same steps with rich output in between; the webhook server calls this.
"""

from docdrift.config import Settings
from docdrift.pr_writer import PublishResult


class SyncConfigError(RuntimeError):
    pass


def sync_pull_request(
    settings: Settings, repo: str, number: int, min_confidence: float = 0.5, top_k: int = 10
) -> PublishResult:
    """Raises OwnPullRequestError for DocDrift's own PRs and SyncConfigError for missing secrets."""
    from docdrift.analyzer import Analyzer
    from docdrift.github_client import GitHubClient
    from docdrift.github_writer import GitHubWriter
    from docdrift.llm import JsonLLM, LLMError
    from docdrift.pipeline import analyze_pull_request
    from docdrift.pr_writer import PRWriter

    if not settings.github_token:
        raise SyncConfigError("GITHUB_TOKEN is required to open a docs PR")
    try:
        llm = JsonLLM.from_settings(settings)
    except LLMError as exc:
        raise SyncConfigError(str(exc)) from exc

    result = analyze_pull_request(GitHubClient(settings.github_token), repo, number, top_k=top_k)
    proposals = Analyzer(llm).propose(result, min_confidence=min_confidence)
    return PRWriter(GitHubWriter(settings.github_token)).publish(result, proposals, min_confidence)
