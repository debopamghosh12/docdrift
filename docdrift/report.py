"""Rich terminal rendering of an AnalysisResult."""

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from docdrift.models import ChangeKind, SymbolChange
from docdrift.pipeline import AnalysisResult

_CHANGE_STYLES = {
    ChangeKind.ADDED: "green",
    ChangeKind.REMOVED: "red",
    ChangeKind.MODIFIED: "yellow",
    ChangeKind.SIGNATURE_CHANGED: "magenta",
    ChangeKind.RENAMED: "cyan",
}


def render_analysis(result: AnalysisResult, console: Console | None = None) -> None:
    console = console or Console()
    pr = result.pr
    console.print(
        Panel(
            f"[bold]{pr.title}[/bold]\n{pr.url}\n"
            f"merge {pr.merge_commit_sha[:7]} | {len(pr.files)} files changed | "
            f"{len(result.sections)} doc sections indexed",
            title=f"{pr.repo}#{pr.number}",
        )
    )
    console.print(_symbols_table(result))
    console.print(_interfaces_table(result))
    console.print(_candidates_table(result))
    console.print(_missing_table(result))


def _change(kind: ChangeKind) -> str:
    return f"[{_CHANGE_STYLES[kind]}]{kind.value}[/]"


def _signature(symbol: SymbolChange) -> str:
    if symbol.change == ChangeKind.RENAMED:
        renamed = f"{symbol.old_name} -> {symbol.name}"
        if symbol.old_signature != symbol.new_signature:
            renamed += f"\n{symbol.old_signature}\n-> {symbol.new_signature}"
        return renamed
    if symbol.change == ChangeKind.SIGNATURE_CHANGED:
        return f"{symbol.old_signature}\n-> {symbol.new_signature}"
    return symbol.new_signature or symbol.old_signature or ""


def _symbols_table(result: AnalysisResult) -> Table:
    table = Table(title="Changed symbols", expand=True)
    for column in ("Change", "Kind", "Symbol", "Signature", "File"):
        table.add_column(column)
    for s in result.changeset.symbols:
        name = s.name if s.public else f"[dim]{s.name}[/dim]"
        if s.route:
            name += f"\n[dim]handler of {s.route}[/dim]"
        table.add_row(_change(s.change), s.kind.value, name, _signature(s), s.file)
    if not result.changeset.symbols:
        table.add_row("[dim]none[/dim]", "", "", "", "")
    return table


def _interfaces_table(result: AnalysisResult) -> Table:
    cs = result.changeset
    table = Table(title="Env vars, routes & CLI flags", expand=True)
    for column in ("Type", "Change", "Name", "File"):
        table.add_column(column)
    for env in cs.env_vars:
        table.add_row("env var", _change(env.change), env.name, env.file)
    for route in cs.routes:
        name = route.label
        if route.old_label:
            name = f"{route.old_label} -> {route.label}"
        elif route.change == ChangeKind.MODIFIED and route.handler:
            name += f"\n[dim]handler {route.handler} changed[/dim]"
        table.add_row("route", _change(route.change), name, route.file)
    for flag in cs.cli_flags:
        table.add_row("cli flag", _change(flag.change), flag.flag, flag.file)
    if not (cs.env_vars or cs.routes or cs.cli_flags):
        table.add_row("[dim]none[/dim]", "", "", "")
    return table


def _candidates_table(result: AnalysisResult) -> Table:
    table = Table(title="Candidate stale sections", expand=True)
    table.add_column("#", justify="right")
    table.add_column("Score", justify="right")
    table.add_column("Section")
    table.add_column("Location")
    table.add_column("Matched terms")
    for rank, c in enumerate(result.candidates, start=1):
        heading = " > ".join(c.section.heading_path) or c.section.heading
        table.add_row(str(rank), f"{c.score:.1f}", heading, c.section.location, ", ".join(c.matched_terms))
    if not result.candidates:
        table.add_row("", "", "[dim]no stale candidates[/dim]", "", "")
    return table


def _missing_table(result: AnalysisResult) -> Table:
    table = Table(title="Missing docs (new and never mentioned)", expand=True)
    for column in ("Kind", "Name", "Defined in"):
        table.add_column(column)
    for m in result.missing:
        table.add_row(m.kind, m.name, m.file)
    if not result.missing:
        table.add_row("[dim]none[/dim]", "", "")
    return table
