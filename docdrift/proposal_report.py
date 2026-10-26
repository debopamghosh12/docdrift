"""Rich rendering of LLM proposals and the resulting doc diffs."""

from rich.console import Console
from rich.table import Table
from rich.text import Text

from docdrift.analyzer import DocProposals

_DIFF_STYLES = (("+++", "bold"), ("---", "bold"), ("@@", "cyan"), ("+", "green"), ("-", "red"))


def render_proposals(proposals: DocProposals, min_confidence: float, console: Console) -> None:
    console.print(_verdicts_table(proposals, min_confidence))
    console.print(_insertions_table(proposals, min_confidence))
    for error in proposals.errors:
        console.print(f"[red]LLM error:[/] {error}", markup=True, highlight=False)


def render_diffs(diffs: dict[str, str], console: Console) -> None:
    if not diffs:
        console.print("[dim]No documentation changes proposed.[/]")
        return
    for path, diff in diffs.items():
        console.rule(f"[bold]{path}")
        text = Text()
        for line in diff.splitlines():
            style = next((s for prefix, s in _DIFF_STYLES if line.startswith(prefix)), "")
            text.append(line + "\n", style=style)
        console.print(text, soft_wrap=True)


def _applied(confidence: float, min_confidence: float) -> str:
    return "[green]yes[/]" if confidence >= min_confidence else "[yellow]below threshold[/]"


def _verdicts_table(proposals: DocProposals, min_confidence: float) -> Table:
    table = Table(title="LLM verdicts on candidate sections", expand=True)
    table.add_column("Section")
    table.add_column("Stale")
    table.add_column("Conf.", justify="right")
    table.add_column("Applied")
    table.add_column("Reason", ratio=2)
    for f in proposals.findings:
        v = f.verdict
        stale = "[red]stale[/]" if v.stale else "[green]ok[/]"
        applied = _applied(v.confidence, min_confidence) if v.stale else "-"
        table.add_row(f.section.location, stale, f"{v.confidence:.2f}", applied, v.reason)
    if not proposals.findings:
        table.add_row("[dim]none[/dim]", "", "", "", "")
    return table


def _insertions_table(proposals: DocProposals, min_confidence: float) -> Table:
    table = Table(title="Proposed new sections for missing docs", expand=True)
    table.add_column("Item")
    table.add_column("Insert into")
    table.add_column("Conf.", justify="right")
    table.add_column("Applied")
    table.add_column("Reason", ratio=2)
    for ins in proposals.insertions:
        p = ins.proposal
        where = f"{p.file} after '{p.after_heading}'" if p.after_heading else f"end of {p.file}"
        item = f"{ins.item.kind} {ins.item.name}"
        table.add_row(item, where, f"{p.confidence:.2f}", _applied(p.confidence, min_confidence), p.reason)
    for item in proposals.covered:
        table.add_row(f"{item.kind} {item.name}", "[dim]covered by a section rewrite[/dim]", "", "-", "")
    if not (proposals.insertions or proposals.covered):
        table.add_row("[dim]none[/dim]", "", "", "", "")
    return table
