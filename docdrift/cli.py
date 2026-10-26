"""Command-line interface for DocDrift."""

import sys

import typer
from rich.console import Console

from docdrift import __version__

app = typer.Typer(help="DocDrift: keep documentation in sync with code.", no_args_is_help=True)


@app.command()
def version() -> None:
    """Print the DocDrift version."""
    typer.echo(__version__)


@app.command()
def run(
    repo: str = typer.Option(..., "--repo", help="Repository in owner/name form."),
    pr: int = typer.Option(..., "--pr", help="Merged PR number to analyze."),
    top: int = typer.Option(10, "--top", help="How many candidate sections to keep."),
) -> None:
    """Analyze a merged PR: show its ChangeSet and the doc sections likely to be stale."""
    from github import GithubException

    from docdrift.config import get_settings
    from docdrift.github_client import GitHubClient, PullRequestNotMergedError
    from docdrift.pipeline import analyze_pull_request
    from docdrift.report import render_analysis

    _force_utf8_stdout()
    console = Console()
    settings = get_settings()
    if not settings.github_token:
        console.print("[yellow]GITHUB_TOKEN is not set; using the unauthenticated API (60 req/h).[/]")
    client = GitHubClient(settings.github_token or None)
    try:
        with console.status(f"Analyzing {repo}#{pr}..."):
            result = analyze_pull_request(client, repo, pr, top_k=top)
    except PullRequestNotMergedError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(code=1) from exc
    except GithubException as exc:
        message = exc.data.get("message") if isinstance(exc.data, dict) else exc
        console.print(f"[red]GitHub API error ({exc.status}): {message}[/]")
        raise typer.Exit(code=1) from exc
    render_analysis(result, console)


def _force_utf8_stdout() -> None:
    """PR titles often contain emoji, which crash Windows' default cp1252 console encoding."""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", help="Bind host."),
    port: int = typer.Option(8000, help="Bind port."),
) -> None:
    """Start the webhook server."""
    import uvicorn

    uvicorn.run("docdrift.server:app", host=host, port=port)


if __name__ == "__main__":
    app()
