"""Command-line interface for DocDrift."""

import typer

from docdrift import __version__

app = typer.Typer(help="DocDrift: keep documentation in sync with code.", no_args_is_help=True)


@app.command()
def version() -> None:
    """Print the DocDrift version."""
    typer.echo(__version__)


@app.command()
def run(
    repo: str = typer.Argument(..., help="Repository in owner/name form."),
    pr: int = typer.Argument(..., help="Merged PR number to analyze."),
) -> None:
    """Run the doc-drift pipeline for a merged PR."""
    typer.echo(f"Pipeline not implemented yet (repo={repo}, pr={pr}).")
    raise typer.Exit(code=1)


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
