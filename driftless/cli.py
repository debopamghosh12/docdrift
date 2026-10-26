"""Command-line interface for Driftless."""

import sys
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING

import typer
from rich.console import Console

from driftless import __version__

if TYPE_CHECKING:
    from driftless.llm import JsonLLM
    from driftless.pipeline import AnalysisResult
    from driftless.proposals import DocProposals

app = typer.Typer(help="Driftless: Docs that never drift. Driftless runs your docs to prove they work.", no_args_is_help=True)


@app.command()
def version() -> None:
    """Print the Driftless version."""
    typer.echo(__version__)


@app.command()
def run(
    repo: str = typer.Option(..., "--repo", help="Repository in owner/name form."),
    pr: int = typer.Option(..., "--pr", help="Merged PR number to analyze."),
    top: int = typer.Option(10, "--top", help="How many candidate sections to keep."),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print the proposed doc changes as a diff; don't open a PR or comment."
    ),
    min_confidence: float = typer.Option(
        0.5, "--min-confidence", min=0.0, max=1.0, help="Drop LLM proposals below this confidence."
    ),
) -> None:
    """Sync docs with a merged PR: find stale sections, rewrite and verify them, open a docs PR."""
    from driftless.config import get_settings
    from driftless.github_client import GitHubClient
    from driftless.llm import JsonLLM, LLMError
    from driftless.pipeline import analyze_pull_request
    from driftless.report import render_analysis

    _force_utf8_stdout()
    console = Console()
    settings = get_settings()
    if not settings.github_token:
        if not dry_run:
            console.print("[red]GITHUB_TOKEN is required to open a docs PR (or pass --dry-run).[/]")
            raise typer.Exit(code=1)
        console.print("[yellow]GITHUB_TOKEN is not set; using the unauthenticated API (60 req/h).[/]")
    try:
        llm = JsonLLM.from_settings(settings)
    except LLMError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(code=1) from exc

    client = GitHubClient(settings.github_token or None)
    with _exit_on_errors(console), console.status(f"Analyzing {repo}#{pr}..."):
        result = analyze_pull_request(client, repo, pr, top_k=top)
    render_analysis(result, console)
    proposals = _propose(result, llm, min_confidence, console)
    if dry_run:
        return

    from driftless.github_writer import GitHubWriter
    from driftless.pr_writer import PublishAction, PRWriter

    with _exit_on_errors(console), console.status("Publishing..."):
        published = PRWriter(GitHubWriter(settings.github_token)).publish(result, proposals, min_confidence)
    for path in published.skipped_files:
        console.print(f"[yellow]Skipped {path}: it changed on the default branch since the merge.[/]")
    if published.action == PublishAction.COMMENTED:
        console.print(f"[green]No doc changes to commit; commented on the source PR:[/] {published.url}")
    else:
        console.print(f"[green]Docs PR {published.action.value} ({published.branch}):[/] {published.url}")


@contextmanager
def _exit_on_errors(console: Console) -> Iterator[None]:
    """Turn expected failures into a one-line message and a non-zero exit (0 for our own PRs)."""
    import httpx
    from github import GithubException

    from driftless.github_client import PullRequestNotMergedError
    from driftless.pipeline import OwnPullRequestError

    try:
        yield
    except OwnPullRequestError as exc:
        console.print(f"[yellow]{exc}[/]")
        raise typer.Exit(code=0) from exc
    except PullRequestNotMergedError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(code=1) from exc
    except GithubException as exc:
        message = exc.data.get("message") if isinstance(exc.data, dict) else exc
        console.print(f"[red]GitHub API error ({exc.status}): {message}[/]")
        raise typer.Exit(code=1) from exc
    except httpx.HTTPError as exc:
        console.print(f"[red]Could not download the repository snapshot: {exc}[/]")
        raise typer.Exit(code=1) from exc


def _propose(
    result: "AnalysisResult", llm: "JsonLLM", min_confidence: float, console: Console
) -> "DocProposals":
    from driftless.analyzer import Analyzer
    from driftless.doc_editor import apply_proposals, unified_diff
    from driftless.proposal_report import render_diffs, render_proposals

    with console.status(
        f"Reviewing {len(result.candidates)} sections and {len(result.missing)} missing items..."
    ):
        proposals = Analyzer(llm).propose(result, min_confidence=min_confidence)
    render_proposals(proposals, min_confidence, console)

    updated = apply_proposals(result.docs, proposals, min_confidence)
    originals = {d.path: d.content for d in result.docs}
    render_diffs({p: unified_diff(p, originals[p], new) for p, new in updated.items()}, console)
    return proposals


def _force_utf8_stdout() -> None:
    """PR titles often contain emoji, which crash Windows' default cp1252 console encoding."""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", help="Bind host."),
    port: int = typer.Option(8000, help="Bind port."),
) -> None:
    """Start the GitHub webhook server (POST /webhook, GET /health, GET /runs)."""
    import logging

    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    uvicorn.run("driftless.server:create_app", factory=True, host=host, port=port)


if __name__ == "__main__":
    app()
