"""Markdown for the pre-merge PR comment and the commit status that goes with it."""

import re
from dataclasses import dataclass

from driftless.doc_editor import unified_diff
from driftless.example_impact import ExampleImpact, broken_phrase, consequence_sentence, example_impact
from driftless.feedback import remembered_line
from driftless.execution import ExampleOutcome, ExampleStatus, ExecutionReport
from driftless.pipeline import AnalysisResult
from driftless.pr_body import (
    FOOTER,
    ChangeRow,
    _cell,
    _first_sentence,
    _plural,
    change_rows,
    checked_sentence,
    render_execution,
    status_label,
)
from driftless.proposals import DocProposals

APPLY_COMMAND = "/driftless apply"
# GitHub rejects comments over 65,536 characters; leave room for everything around the diffs.
MAX_DIFF_CHARS = 40_000


def premerge_marker(number: int) -> str:
    """Hidden marker of the one pre-merge comment per PR (edited in place on every push)."""
    return f"<!-- driftless:pre-merge={number} -->"


@dataclass(frozen=True)
class CheckSummary:
    stale: int  # doc sections this PR makes stale: rewrites and new sections
    broken: int  # distinct examples this PR breaks (root causes, fixed after running or not)
    errors: int  # reviews that could not finish (LLM errors)
    pre_existing: int = 0  # already wrong at the base commit: shown, never counted against the PR
    broken_labels: tuple[str, ...] = ()

    @property
    def state(self) -> str:
        """Commit status state."""
        if self.stale or self.broken:
            return "failure"
        return "error" if self.errors else "success"

    @property
    def description(self) -> str:
        """Commit status description (GitHub allows 140 characters)."""
        if self.state == "success":
            already = f" ({_plural(self.pre_existing, 'pre-existing issue')} on the base branch)" if self.pre_existing else ""
            return f"Docs are in sync with this PR{already}"
        if self.state == "error":
            return f"Could not finish checking the docs ({_plural(self.errors, 'error')})"
        parts = []
        if self.stale:
            parts.append(_plural(self.stale, "stale doc section"))
        if self.broken:
            parts.append(f"{_plural(self.broken, 'broken example')} ({', '.join(self.broken_labels)})")
        text = ", ".join(parts)
        return text if len(text) <= 140 else text[:139] + "…"


@dataclass(frozen=True)
class Drift:
    """Stale sections and broken examples, split into the PR's own and pre-existing ones."""

    rows: list[ChangeRow]
    examples: ExampleImpact

    @property
    def fixed(self) -> list[ExampleOutcome]:
        return [o for o in self.examples.roots if o.status == ExampleStatus.FIXED]

    @property
    def failing(self) -> list[ExampleOutcome]:
        return [o for o in self.examples.roots if o.status == ExampleStatus.FAILING]

    @property
    def count(self) -> int:
        return len(self.rows) + len(self.examples.roots)

    def __bool__(self) -> bool:
        return bool(self.rows or self.examples)


def split_drift(proposals: DocProposals, min_confidence: float) -> tuple[Drift, Drift]:
    """(introduced by this PR, already there at the base commit)."""
    rows = change_rows(proposals, min_confidence)
    outcomes = (proposals.execution or ExecutionReport()).outcomes

    def part(pre_existing: bool) -> Drift:
        return Drift(
            rows=[r for r in rows if r.pre_existing == pre_existing],
            examples=example_impact([o for o in outcomes if o.pre_existing == pre_existing]),
        )

    return part(False), part(True)


def summarize(proposals: DocProposals, min_confidence: float) -> CheckSummary:
    introduced, pre_existing = split_drift(proposals, min_confidence)
    return CheckSummary(
        stale=len(introduced.rows),
        broken=len(introduced.examples.labels),
        errors=len(proposals.errors),
        pre_existing=pre_existing.count,
        broken_labels=tuple(introduced.examples.labels),
    )


def render_premerge_comment(
    result: AnalysisResult, proposals: DocProposals, min_confidence: float, fixes: dict[str, str]
) -> str:
    """`fixes`: the verified new content of every doc file `/driftless apply` would commit."""
    pr = result.pr
    check = summarize(proposals, min_confidence)
    introduced, pre_existing = split_drift(proposals, min_confidence)
    execution = proposals.execution
    sha = pr.head_sha[:7]
    parts = [premerge_marker(pr.number)]

    if check.state == "success":
        ran = f" {_examples(execution.total, 'pass')} against the app." if execution and execution.total else ""
        if check.pre_existing:
            ran = f" {_plural(check.pre_existing, 'issue')} already on `{pr.base_ref}` are listed below."
        parts += [
            "## ✅ Docs are in sync with this PR",
            "",
            f"Driftless {_lower_first(checked_sentence(result, execution))} at `{sha}`: nothing this PR changes "
            f"is stale and no new interface is undocumented.{ran}",
        ]
    elif check.stale or check.broken:
        parts += [f"## ⚠️ {_headline(check, introduced.examples)}", ""]
        consequence = consequence_sentence(introduced.examples)
        if consequence:
            parts += [consequence + (" They are listed under the executed examples below." if introduced.examples.dependent else ""), ""]
        parts.append(f"Driftless compared the docs with the code at `{sha}`. Fix them before merging:")
    else:
        parts += ["## ⚠️ Driftless could not finish checking the docs", ""]

    parts += _drift_table(introduced)
    if pre_existing:
        parts += [
            "",
            "<details>",
            f"<summary>Already stale on {pr.base_ref}, not caused by this PR ({check.pre_existing})</summary>",
            "",
            f"These were already wrong at the base commit `{pr.merge_base_sha[:7]}`, so they don't fail this "
            "check. Their fixes are still in the diff below.",
            *_drift_table(pre_existing),
            "",
            "</details>",
        ]
    parts += _diffs(result, fixes)
    if execution:
        parts += ["", *render_execution(execution)]
    if proposals.errors:
        parts += ["", "**Errors**", "", *[f"- {_cell(e)}" for e in proposals.errors]]
    if fixes:
        if pr.head_repo and pr.head_repo.lower() != pr.repo.lower():
            parts += ["", "This PR comes from a fork, so Driftless can't push to it: apply the diff above by hand."]
        else:
            parts += ["", f"Reply `{APPLY_COMMAND}` to commit these doc fixes to this branch."]
    remembered = remembered_line(result, proposals)
    if remembered:
        parts += ["", remembered]
    parts += ["", "---", f"<sub>Driftless checked `{sha}` · this comment updates on every push</sub>", FOOTER, ""]
    return "\n".join(parts)


def render_apply_reply(user: str, branch: str, commit_url: str, files: list[str]) -> str:
    listed = ", ".join(f"`{p}`" for p in files)
    return (
        f"✅ @{user} Driftless committed the verified doc fixes to `{branch}` in {commit_url} ({listed}). "
        "The check reruns on this push.\n\n---\n" + FOOTER + "\n"
    )


def render_apply_declined(user: str, reason: str) -> str:
    return f"⚠️ @{user} Driftless did not apply the doc fixes: {reason}\n\n---\n{FOOTER}\n"


def _headline(check: CheckSummary, examples: ExampleImpact) -> str:
    """'This PR breaks 1 documented example (`POST /notes`) in 2 files', and/or stale sections."""
    sections = _plural(check.stale, "doc section")
    if not check.broken:
        return f"This PR will make {sections} stale"
    broken = broken_phrase(examples)
    if not check.stale:
        return f"This PR breaks {broken}"
    return f"This PR makes {sections} stale and breaks {broken}"


def _example_fix(outcome: ExampleOutcome) -> str:
    if outcome.status == ExampleStatus.FIXED:
        return "🔁 fixed after running"
    if outcome.needs_human:
        return f"❌ needs a human: {_cell(outcome.needs_human)}"
    return "❌ no fix found"


def _lower_first(text: str) -> str:
    return text[:1].lower() + text[1:]


def _drift_table(drift: Drift) -> list[str]:
    """The PR's own problems: stale sections and root-cause examples (knock-ons are listed with
    the executed examples)."""
    if not (drift.rows or drift.fixed or drift.failing):
        return []
    lines = ["", "| File | Section | Why | Proposed fix |", "|------|---------|-----|--------------|"]
    lines += [
        f"| `{r.file}` | {_cell(r.section)} | {_cell(_first_sentence(r.why))} | {r.change}: {_cell(status_label(r.verification))} |"
        for r in drift.rows
    ]
    lines += [
        f"| `{o.file}` | example `{_cell(o.label)}` (line {o.line}) | fails when run: {_cell('; '.join(o.failures))} "
        f"| {_example_fix(o)} |"
        for o in [*drift.fixed, *drift.failing]
    ]
    return lines


def _diffs(result: AnalysisResult, fixes: dict[str, str]) -> list[str]:
    if not fixes:
        return []
    originals = {d.path: d.content for d in result.docs}
    lines = ["", "### Proposed doc changes", ""]
    budget = MAX_DIFF_CHARS
    for path, new in sorted(fixes.items()):
        diff = unified_diff(path, originals.get(path, ""), new)
        added = sum(1 for line in diff.splitlines() if line.startswith("+") and not line.startswith("+++"))
        removed = sum(1 for line in diff.splitlines() if line.startswith("-") and not line.startswith("---"))
        if len(diff) > budget:
            lines += [f"- `{path}` (+{added} −{removed}): diff too long to show here"]
            continue
        budget -= len(diff)
        fence = _fence(diff)
        lines += [
            "<details>",
            f"<summary><code>{path}</code> (+{added} −{removed})</summary>",
            "",
            f"{fence}diff",
            diff.rstrip("\n"),
            fence,
            "",
            "</details>",
        ]
    return lines


def _examples(n: int, verb: str, every: bool = True) -> str:
    """'The 1 documented example passes' / 'All 3 documented examples pass' (`verb` is plural)."""
    if n == 1:
        singular = verb + ("es" if verb.endswith("s") else "s")
        return f"{'The 1' if every else '1'} documented example {singular}"
    return f"{'All ' if every else ''}{n} documented examples {verb}"


def _fence(text: str) -> str:
    """A backtick fence longer than any backtick run in `text` (doc diffs contain ``` fences)."""
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    return "`" * max(3, longest + 1)
