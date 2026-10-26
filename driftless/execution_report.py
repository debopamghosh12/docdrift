"""Rich rendering of executed doc examples."""

from rich.console import Console
from rich.markup import escape
from rich.table import Table

from driftless.execution import ExampleOutcome, ExampleStatus, ExecutionReport

_LABELS = {
    ExampleStatus.PASSED: "[green]🧪 passed[/]",
    ExampleStatus.FIXED: "[cyan]🔁 fixed after running[/]",
    ExampleStatus.FAILING: "[red]❌ still failing[/]",
    ExampleStatus.DEPENDENT: "[yellow]⛓️ failed only because of an earlier example[/]",
}


def render_execution(report: ExecutionReport, console: Console, title: str = "Executed examples") -> None:
    for error in report.errors:
        console.print(f"[red]Sandbox:[/] {escape(error)}")
    if not report.outcomes:
        console.print("[dim]No runnable examples (curl or http request blocks against localhost) found.[/]")
        return
    app = f" against {report.app}" if report.app else ""
    table = Table(title=f"{title}{app}: {report.passing}/{report.total} passing", expand=True)
    table.add_column("File")
    table.add_column("Example")
    table.add_column("Expected", justify="right")
    table.add_column("Actual", justify="right")
    table.add_column("Result")
    for outcome in report.outcomes:
        table.add_row(
            f"{escape(outcome.file)}:{outcome.line}",
            escape(outcome.label),
            _expected(outcome),
            _code(outcome.final_actual_status),
            _details(outcome),
        )
    console.print(table)


def _details(outcome: ExampleOutcome) -> str:
    lines = [_LABELS[outcome.status]]
    if outcome.status == ExampleStatus.FIXED:
        lines += [f"[dim]was: {escape(f)}[/]" for f in outcome.failures]
        lines += [f"[dim]fix: {escape(f)}[/]" for f in outcome.fixes]
    elif outcome.status == ExampleStatus.FAILING:
        lines += [f"[dim]{escape(f)}[/]" for f in outcome.failures]
        if outcome.note:
            lines.append(f"[dim]{escape(outcome.note)}[/]")
    elif outcome.status == ExampleStatus.DEPENDENT:
        lines.append(f"[dim]because of {escape(outcome.caused_by or 'an earlier example')}[/]")
        lines += [f"[dim]was: {escape(f)}[/]" for f in outcome.failures]
    lines += [f"[yellow]warn: {escape(w)}[/]" for w in outcome.warnings]
    return "\n".join(lines)


def _expected(outcome: ExampleOutcome) -> str:
    final = _code(outcome.final_expected_status)
    if outcome.expected_status != outcome.final_expected_status:
        return f"[strike dim]{_code(outcome.expected_status)}[/] {final}"
    return final


def _code(status: int | None) -> str:
    return str(status) if status is not None else "-"
