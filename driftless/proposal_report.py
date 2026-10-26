"""Rich rendering of LLM proposals, their verification status, and the resulting doc diffs."""

from rich.console import Console
from rich.markup import escape
from rich.table import Table
from rich.text import Text

from driftless.proposals import DocProposals, Verification, VerificationStatus

_DIFF_STYLES = (("+++", "bold"), ("---", "bold"), ("@@", "cyan"), ("+", "green"), ("-", "red"))


def render_proposals(proposals: DocProposals, min_confidence: float, console: Console) -> None:
    console.print(_verdicts_table(proposals, min_confidence))
    console.print(_insertions_table(proposals, min_confidence))
    for error in proposals.errors:
        console.print(f"[red]LLM error:[/] {escape(error)}")


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


def verification_label(verification: Verification | None, confidence: float, min_confidence: float) -> str:
    if confidence < min_confidence:
        return "[yellow]below confidence threshold[/]"
    if verification is None:
        return "-"
    lines: list[str] = []
    if verification.status == VerificationStatus.VERIFIED:
        lines.append("[green]✅ verified[/]")
    elif verification.status == VerificationStatus.FIXED_ON_RETRY:
        lines.append("[cyan]🔁 fixed on retry[/]")
        lines += [f"[dim]was: {escape(f)}[/]" for f in verification.failures]
    else:
        lines.append("[red]⚠️ dropped[/]")
        reasons = verification.retry_failures or verification.failures
        lines += [f"[dim]{escape(r)}[/]" for r in reasons]
        if verification.note:
            lines.append(f"[dim]{escape(verification.note)}[/]")
    if verification.restored_lines:
        lines.append(f"[dim]restored {verification.restored_lines} cosmetically changed line(s)[/]")
    lines += [f"[yellow]flag: {escape(f)}[/]" for f in verification.flags]
    return "\n".join(lines)


def _verdicts_table(proposals: DocProposals, min_confidence: float) -> Table:
    table = Table(title="LLM verdicts on candidate sections", expand=True)
    table.add_column("Section")
    table.add_column("Stale")
    table.add_column("Conf.", justify="right")
    table.add_column("Verification", ratio=2)
    table.add_column("Reason", ratio=2)
    for f in proposals.findings:
        v = f.verdict
        stale = "[red]stale[/]" if v.stale else "[green]ok[/]"
        status = verification_label(f.verification, v.confidence, min_confidence) if v.stale else "-"
        table.add_row(f.section.location, stale, f"{v.confidence:.2f}", status, escape(v.reason))
    if not proposals.findings:
        table.add_row("[dim]none[/dim]", "", "", "", "")
    return table


def _insertions_table(proposals: DocProposals, min_confidence: float) -> Table:
    table = Table(title="Proposed new sections for missing docs", expand=True)
    table.add_column("Item")
    table.add_column("Insert into")
    table.add_column("Conf.", justify="right")
    table.add_column("Verification", ratio=2)
    table.add_column("Reason", ratio=2)
    for ins in proposals.insertions:
        p = ins.proposal
        where = f"{p.file} after '{p.after_heading}'" if p.after_heading else f"end of {p.file}"
        status = verification_label(ins.verification, p.confidence, min_confidence)
        table.add_row(
            f"{ins.item.kind} {escape(ins.item.name)}", escape(where), f"{p.confidence:.2f}", status, escape(p.reason)
        )
    for covered in proposals.covered:
        table.add_row(
            f"{covered.item.kind} {escape(covered.item.name)}",
            f"[dim]{covered.file}: covered by a section rewrite[/dim]",
            "",
            "-",
            "",
        )
    if not (proposals.insertions or proposals.covered):
        table.add_row("[dim]none[/dim]", "", "", "", "")
    return table
