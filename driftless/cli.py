"""Command-line interface for Driftless."""

import sys
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING

import typer
from rich.console import Console

from driftless import __version__

if TYPE_CHECKING:
    from driftless.audit import AuditResult
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
    from driftless.memory import Memory
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
    memory = Memory(settings.database_url)
    with _exit_on_errors(console), console.status(f"Analyzing {repo}#{pr}..."):
        result = analyze_pull_request(client, repo, pr, top_k=top, memory=memory.repo_memory(repo))
    render_analysis(result, console)
    proposals = _propose(result, llm, min_confidence, console)
    if execute and settings.execute_examples:
        files = client.fetch_files(repo, result.snapshot_ref)
        proposals.execution = _execute_all(result, proposals, files, llm, min_confidence, settings, console)
    from driftless.feedback import remembered_line

    remembered = remembered_line(result, proposals)
    if remembered:
        console.print(remembered, markup=False)
    if dry_run:
        return

    from driftless.github_writer import GitHubWriter
    from driftless.pr_writer import PublishAction, PRWriter
    from driftless.sync import remember_docs_pr

    with _exit_on_errors(console), console.status("Publishing..."):
        published = PRWriter(GitHubWriter(settings.github_token)).publish(result, proposals, min_confidence)
    remember_docs_pr(memory, result, proposals, min_confidence, published)
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
    from driftless.doc_editor import proposed_docs
    from driftless.example_fixer import ExampleFixer
    from driftless.executable_docs import execute_all_docs
    from driftless.sandbox import SandboxLimits
    from driftless.execution_report import render_execution
    from driftless.sync import executable_docs

    with console.status("Running the examples in all docs against the app...") as status:
        report = execute_all_docs(
            executable_docs(result),
            files,
            proposals,
            min_confidence,
            ExampleFixer(llm),
            SandboxLimits.from_settings(settings),
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
    from driftless.audit import AuditConfigError, run_audit
    from driftless.config import get_settings
    from driftless.execution_report import render_execution

    _force_utf8_stdout()
    console = Console()
    settings = get_settings()
    with _exit_on_errors(console), console.status(f"Downloading {repo}@{ref}...") as status:
        try:
            audit = run_audit(settings, repo, ref, fix, progress=lambda stage, detail: status.update(detail))
        except AuditConfigError as exc:
            console.print(f"[red]{exc}[/]")
            raise typer.Exit(code=1) from exc
    report = audit.report
    render_execution(report, console, title=f"Docs health audit of {repo}@{ref}")
    if fix:
        _render_example_fixes(audit.originals, report, console)
        _print_published_fixes(audit, console)
    if report.failing or report.errors:
        raise typer.Exit(code=1)


def _print_published_fixes(audit: "AuditResult", console: Console) -> None:
    published = audit.published
    if published is None:
        console.print(f"[yellow]{audit.publish_note}[/]")
        return
    for path in published.skipped_files:
        console.print(f"[yellow]Skipped {path}: it changed on the default branch since {audit.ref}.[/]")
    console.print(f"[green]Examples PR {published.action.value} ({published.branch}):[/] {published.url}")


def _force_utf8_stdout() -> None:
    """PR titles often contain emoji, which crash Windows' default cp1252 console encoding."""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")


@app.command()
def init() -> None:
    """Ask for GITHUB_TOKEN and LLM_API_KEY (hidden) and write them to .env in this folder."""
    from pathlib import Path

    from dotenv import dotenv_values

    from driftless.init_env import REQUIRED_SECRETS, env_is_ignored, write_env

    _force_utf8_stdout()
    console = Console()
    folder = Path.cwd()
    existing = dotenv_values(folder / ".env") if (folder / ".env").exists() else {}
    hints = {
        "GITHUB_TOKEN": "a GitHub token with access to your repos (https://github.com/settings/tokens)",
        "LLM_API_KEY": "your LLM key; the default endpoint is Groq (https://console.groq.com/keys)",
    }
    values: dict[str, str] = {}
    for key in REQUIRED_SECRETS:
        keep = " (Enter keeps the current one)" if existing.get(key) else ""
        console.print(f"[bold]{key}[/]: {hints[key]}{keep}")
        value = typer.prompt(key, default="", show_default=False, hide_input=True).strip()
        if value:
            values[key] = value
        elif not existing.get(key):
            console.print(f"[yellow]{key} left empty; set it later in .env.[/]")
    path = write_env(folder, values)
    console.print(f"[green]Wrote {path}[/] (LLM_BASE_URL and LLM_MODEL default to Groq's openai/gpt-oss-120b).")
    ignored = env_is_ignored(folder)
    if ignored is False:
        console.print("[bold yellow]⚠ .env is not gitignored here. Add it before committing:[/] echo .env >> .gitignore")
    elif ignored is None:
        console.print("[yellow]Keep .env out of version control (add `.env` to .gitignore).[/]")
    console.print("Next: [bold]driftless doctor --repo owner/name[/]")


@app.command()
def doctor(
    repo: str | None = typer.Option(None, "--repo", help="Also check the token's permissions on this repo (owner/name)."),
) -> None:
    """Check Python, the secrets, the LLM endpoint and the token's GitHub permissions. Prints no secrets."""
    from driftless.config import get_settings
    from driftless.doctor import run_checks

    _force_utf8_stdout()
    console = Console()
    checks = run_checks(get_settings(), repo)
    for check in checks:
        mark = "✅" if check.ok else "❌"
        optional = "" if check.required else " [dim](optional)[/]"
        console.print(f"{mark} [bold]{check.name}[/]{optional}" + (f": {check.detail}" if check.detail else ""), highlight=False)
        if not check.ok and check.fix:
            console.print(f"   [dim]→ {check.fix}[/]", highlight=False)
    if not repo:
        console.print("[dim]Pass --repo owner/name to check the token's permissions on a repository.[/]")
    if any(not c.ok and c.required for c in checks):
        raise typer.Exit(code=1)


@app.command()
def action(
    fail_on_drift: bool = typer.Option(
        True, "--fail-on-drift/--no-fail-on-drift", envvar="DRIFTLESS_FAIL_ON_DRIFT",
        help="Exit 1 when a pre-merge check or audit finds drift (blocks merging as a required check).",
    ),
    fix: bool = typer.Option(False, "--fix", envvar="DRIFTLESS_FIX", help="Audits: fix broken examples and open a PR."),
) -> None:
    """Run inside GitHub Actions: handle the workflow's event (see action.yml)."""
    import logging
    import os

    from driftless.action import ActionError, build_runner, plan_from_env, write_outputs
    from driftless.config import get_settings
    from driftless.events import EventKind
    from driftless.repo_config import RepoConfigError
    from driftless.sync import SyncConfigError

    _force_utf8_stdout()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per request drowns the job log
    console = Console()
    try:
        plan = plan_from_env(os.environ, fix)
        console.print(f"Event routed to: [bold]{plan.kind.value}[/]", highlight=False)
        if plan.kind == EventKind.IGNORED:
            console.print(f"Nothing to do: {plan.reason}.")
            return
        with _exit_on_errors(console):
            runner = build_runner(get_settings(), plan.repo, fail_on_drift, say=lambda text: console.print(text, highlight=False))
            outcome = runner.run(plan)
    except (ActionError, SyncConfigError, RepoConfigError) as exc:
        print(f"::error title=Driftless::{exc}")
        raise typer.Exit(code=1) from exc
    console.print(outcome.summary, highlight=False)
    write_outputs(plan, outcome)
    if outcome.exit_code:
        print(f"::error title=Driftless::{outcome.summary.splitlines()[0]}")
        raise typer.Exit(code=outcome.exit_code)


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", help="Bind host (0.0.0.0 in a container)."),
    port: int = typer.Option(8000, envvar="PORT", help="Bind port (or the PORT env var, as set by Render)."),
) -> None:
    """Start the webhook server and the web app (/, POST /webhook, GET /health, GET /runs)."""
    import logging

    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    uvicorn.run("driftless.server:create_app", factory=True, host=host, port=port)


if __name__ == "__main__":
    app()
