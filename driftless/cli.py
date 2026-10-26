"""Command-line interface for Driftless."""

import sys
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING

import typer
from rich.console import Console

from driftless import __version__

if TYPE_CHECKING:
    from driftless.config import Settings
    from driftless.execution import ExecutionReport
    from driftless.github_client import GitHubClient
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
    execute: bool = typer.Option(
        True, "--execute/--no-execute", help="Run the examples in all docs against the app (runs repo code)."
    ),
) -> None:
    """Sync docs with a merged PR: find stale sections, rewrite and verify them, run examples, open a docs PR."""
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
    if execute and settings.execute_examples:
        files = client.fetch_files(repo, result.pr.merge_commit_sha)
        proposals.execution = _execute_all(result, proposals, files, llm, min_confidence, settings, console)
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


def _execute_all(
    result: "AnalysisResult",
    proposals: "DocProposals",
    files: dict[str, str],
    llm: "JsonLLM",
    min_confidence: float,
    settings: "Settings",
    console: Console,
) -> "ExecutionReport":
    from pathlib import Path

    from driftless.doc_editor import proposed_docs
    from driftless.example_fixer import ExampleFixer
    from driftless.executable_docs import execute_all_docs
    from driftless.execution_report import render_execution

    with console.status("Running the examples in all docs against the app...") as status:
        report = execute_all_docs(
            result.docs,
            files,
            proposals,
            min_confidence,
            ExampleFixer(llm),
            Path(settings.sandbox_cache_dir),
            settings.sandbox_startup_timeout,
            progress=status.update,
        )
    render_execution(report, console, title="Executed examples")
    _render_example_fixes(proposed_docs(result.docs, proposals, min_confidence), report, console)
    return report


def _render_example_fixes(before: dict[str, str], report: "ExecutionReport", console: Console) -> None:
    from driftless.doc_editor import unified_diff
    from driftless.proposal_report import render_diffs

    diffs = {p: unified_diff(p, before[p], t) for p, t in report.contents.items() if p in before and t != before[p]}
    if diffs:
        console.print("[bold cyan]Example fixes after running:[/]")
        render_diffs(diffs, console)


@app.command()
def check(
    repo: str = typer.Option(..., "--repo", help="Repository in owner/name form."),
    ref: str = typer.Option("main", "--ref", help="Branch, tag or commit to audit."),
    fix: bool = typer.Option(False, "--fix", help="Fix broken examples with the LLM and open a PR."),
) -> None:
    """Docs health audit: run every example in the repo's docs against its app and report pass/fail.

    Runs the repository's code locally; only use it on repositories you trust.
    """
    from pathlib import Path

    from driftless.config import get_settings
    from driftless.executable_docs import execute_docs
    from driftless.execution_report import render_execution
    from driftless.github_client import GitHubClient

    _force_utf8_stdout()
    console = Console()
    settings = get_settings()
    if fix and not settings.github_token:
        console.print("[red]GITHUB_TOKEN is required to open a PR with --fix.[/]")
        raise typer.Exit(code=1)
    fixer = None
    if fix:
        from driftless.example_fixer import ExampleFixer
        from driftless.llm import JsonLLM, LLMError

        try:
            fixer = ExampleFixer(JsonLLM.from_settings(settings))
        except LLMError as exc:
            console.print(f"[red]{exc}[/]")
            raise typer.Exit(code=1) from exc

    client = GitHubClient(settings.github_token or None)
    with _exit_on_errors(console), console.status(f"Downloading {repo}@{ref}..."):
        docs = client.fetch_docs(repo, ref)
        files = client.fetch_files(repo, ref)
    originals = {d.path: d.content for d in docs}
    with console.status("Running the examples in the docs against the app...") as status:
        report = execute_docs(
            files, originals, fixer, Path(settings.sandbox_cache_dir), settings.sandbox_startup_timeout, status.update
        )
    render_execution(report, console, title=f"Docs health audit of {repo}@{ref}")
    if fix:
        _render_example_fixes(originals, report, console)
        _publish_example_fixes(client, settings, repo, ref, originals, report, console)
    if report.failing or report.errors:
        raise typer.Exit(code=1)


def _publish_example_fixes(
    client: "GitHubClient",
    settings: "Settings",
    repo: str,
    ref: str,
    originals: dict[str, str],
    report: "ExecutionReport",
    console: Console,
) -> None:
    from driftless.github_writer import GitHubWriter
    from driftless.pr_writer import PRWriter

    if not report.fixed:
        console.print("[dim]No examples were fixed, so no PR was opened.[/]")
        return
    with _exit_on_errors(console), console.status("Publishing..."):
        published = PRWriter(GitHubWriter(settings.github_token)).publish_example_fixes(repo, ref, originals, report)
    if published is None:
        console.print("[yellow]The fixed files changed on the default branch since this ref; no PR opened.[/]")
        return
    for path in published.skipped_files:
        console.print(f"[yellow]Skipped {path}: it changed on the default branch since {ref}.[/]")
    console.print(f"[green]Examples PR {published.action.value} ({published.branch}):[/] {published.url}")


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
