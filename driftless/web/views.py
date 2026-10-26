"""Turn audit reports and PR analyses into rows the templates render.

Labels and summaries come from the same helpers as the PR comments and docs PR bodies, so the
web app says exactly what GitHub would.
"""

from dataclasses import dataclass, field

from driftless.audit import AuditResult
from driftless.doc_editor import final_docs, unified_diff
from driftless.example_impact import broken_phrase, consequence_sentence, example_impact
from driftless.execution import ExampleOutcome, ExampleStatus, ExecutionReport
from driftless.feedback import remembered_line
from driftless.pr_body import ChangeRow, change_rows, checked_sentence, example_result, status_label
from driftless.premerge_body import summarize
from driftless.proposals import VerificationStatus
from driftless.sync import PullAnalysis

TONES = {
    ExampleStatus.PASSED: "green",
    ExampleStatus.FIXED: "cyan",
    ExampleStatus.DEPENDENT: "amber",
    ExampleStatus.FAILING: "red",
}
VERIFICATION_TONES = {
    VerificationStatus.VERIFIED: "green",
    VerificationStatus.FIXED_ON_RETRY: "cyan",
    VerificationStatus.DROPPED: "red",
}


@dataclass
class ExampleRow:
    file: str
    line: int
    label: str
    expected: str
    expected_before: str | None  # the documented status before a fix changed it
    actual: str
    result: str  # "🧪 passed", "🔁 fixed after running", ...
    tone: str
    details: list[str] = field(default_factory=list)
    pre_existing: bool = False


@dataclass
class DiffLine:
    kind: str  # add | del | hunk | meta | context
    text: str


@dataclass
class FileDiff:
    path: str
    lines: list[DiffLine]


@dataclass
class ExamplesView:
    rows: list[ExampleRow]
    total: int
    passing: int
    fixed: int
    failing: int
    dependent: int
    app: str | None
    errors: list[str]
    headline: str
    consequence: str = ""


@dataclass
class ChangeView:
    file: str
    section: str
    change: str  # updated | added
    why: str
    confidence: float
    verification: str
    tone: str
    triggered_by: list[str]
    notes: list[str]
    pre_existing: bool


@dataclass
class AnalysisView:
    pre_merge: bool
    head_sha: str
    ref: str
    checked: str
    state: str  # success | failure | error (open PRs: the commit status it would set)
    headline: str
    changes: list[ChangeView]
    covered: list[str]
    reviewed: int
    up_to_date: int
    diffs: list[FileDiff]
    examples: ExamplesView | None
    remembered: str | None
    errors: list[str]


@dataclass
class CheckView:
    repo: str
    ref: str
    fix: bool
    examples: ExamplesView
    diffs: list[FileDiff]
    publish_note: str | None


def examples_view(report: ExecutionReport) -> ExamplesView:
    rows = [_example_row(o) for o in report.outcomes]
    impact = example_impact(report.outcomes)
    if not report.outcomes:
        headline = "No runnable examples found (curl or http request blocks against localhost)."
    elif not impact.roots:
        headline = f"All {report.total} documented examples pass." if report.total != 1 else "The documented example passes."
    else:
        headline = f"Broken: {broken_phrase(impact)}."
    return ExamplesView(
        rows=rows,
        total=report.total,
        passing=report.passing,
        fixed=len(report.fixed),
        failing=len(report.failing),
        dependent=len(report.dependent),
        app=report.app,
        errors=report.errors,
        headline=headline,
        consequence=consequence_sentence(impact) if impact.roots else "",
    )


def check_view(audit: AuditResult) -> CheckView:
    return CheckView(
        repo=audit.repo,
        ref=audit.ref,
        fix=audit.fix,
        examples=examples_view(audit.report),
        diffs=diff_views(audit.diffs),
        publish_note=audit.publish_note,
    )


def analysis_view(analysis: PullAnalysis) -> AnalysisView:
    result, proposals, min_confidence = analysis.result, analysis.proposals, analysis.min_confidence
    changed = final_docs(result.docs, proposals, min_confidence)
    originals = {d.path: d.content for d in result.docs}
    if analysis.pre_merge:
        check = summarize(proposals, min_confidence)
        state, headline = check.state, check.description
    elif changed:
        state = "failure"
        headline = f"Would open a docs PR changing {len(changed)} file{'s' if len(changed) != 1 else ''}."
    else:
        state = "error" if proposals.errors else "success"
        headline = "Docs are in sync with this PR; Driftless would say so in a comment on it."
    reviewed = proposals.findings
    return AnalysisView(
        pre_merge=analysis.pre_merge,
        head_sha=result.pr.head_sha,
        ref=result.snapshot_ref,
        checked=checked_sentence(result, proposals.execution) + ".",
        state=state,
        headline=headline,
        changes=[_change(r) for r in change_rows(proposals, min_confidence)],
        covered=[f"{c.item.name} ({c.file})" for c in proposals.covered],
        reviewed=len(reviewed),
        up_to_date=sum(1 for f in reviewed if not f.verdict.stale),
        diffs=diff_views({p: unified_diff(p, originals[p], text) for p, text in sorted(changed.items())}),
        examples=examples_view(proposals.execution) if proposals.execution else None,
        remembered=remembered_line(result, proposals),
        errors=proposals.errors,
    )


def diff_views(diffs: dict[str, str]) -> list[FileDiff]:
    return [FileDiff(path, [_diff_line(line) for line in diff.splitlines()]) for path, diff in diffs.items()]


def _diff_line(line: str) -> DiffLine:
    if line.startswith(("+++", "---")):
        return DiffLine("meta", line)
    if line.startswith("@@"):
        return DiffLine("hunk", line)
    if line.startswith("+"):
        return DiffLine("add", line)
    if line.startswith("-"):
        return DiffLine("del", line)
    return DiffLine("context", line)


def _example_row(o: ExampleOutcome) -> ExampleRow:
    details: list[str] = []
    if o.status == ExampleStatus.FIXED:
        details += [f"was: {f}" for f in o.failures] + [f"fix: {f}" for f in o.fixes]
    elif o.status == ExampleStatus.FAILING:
        details += o.failures + ([o.note] if o.note else [])
    elif o.status == ExampleStatus.DEPENDENT:
        details += [f"was: {f}" for f in o.failures]
    details += [f"warning: {w}" for w in o.warnings]
    changed = o.expected_status != o.final_expected_status
    return ExampleRow(
        file=o.file,
        line=o.line,
        label=o.label,
        expected=_code(o.final_expected_status),
        expected_before=_code(o.expected_status) if changed else None,
        actual=_code(o.final_actual_status),
        result=example_result(o),
        tone="red" if o.needs_human else TONES[o.status],
        details=details,
        pre_existing=o.pre_existing,
    )


def _change(row: ChangeRow) -> ChangeView:
    v = row.verification
    notes: list[str] = []
    if v.failures and v.status != VerificationStatus.DROPPED:
        notes.append("first draft failed verification: " + "; ".join(v.failures))
    if v.restored_lines:
        notes.append(f"restored {v.restored_lines} cosmetically changed line(s) to the original text")
    notes += [f"review flag: {flag}" for flag in v.flags]
    return ChangeView(
        file=row.file,
        section=row.section,
        change=row.change,
        why=row.why,
        confidence=row.confidence,
        verification=status_label(v),
        tone=VERIFICATION_TONES[v.status],
        triggered_by=list(row.matched_terms),
        notes=notes,
        pre_existing=row.pre_existing,
    )


def _code(status: int | None) -> str:
    return str(status) if status is not None else "—"
