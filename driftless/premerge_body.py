"""Markdown for the pre-merge PR comment and the commit status that goes with it."""

import re
from dataclasses import dataclass

from driftless.doc_editor import unified_diff
from driftless.execution import ExecutionReport
from driftless.pipeline import AnalysisResult
from driftless.pr_body import FOOTER, _cell, _first_sentence, _plural, change_rows, render_execution, status_label
from driftless.proposals import DocProposals

APPLY_COMMAND = "/driftless apply"
# GitHub rejects comments over 65,536 characters; leave room for everything around the diffs.
MAX_DIFF_CHARS = 40_000


def premerge_marker(number: int) -> str:
    """Hidden marker of the one pre-merge comment per PR (edited in place on every push)."""
    return f"<!-- driftless:pre-merge={number} -->"


@dataclass(frozen=True)
class CheckSummary:
    stale: int  # sections that need a change: rewrites, new sections, examples fixed after running
    failing: int  # examples that still fail
    errors: int  # reviews that could not finish (LLM errors)

    @property
    def state(self) -> str:
        """Commit status state."""
        if self.stale or self.failing:
            return "failure"
        return "error" if self.errors else "success"

    @property
    def description(self) -> str:
        """Commit status description (GitHub allows 140 characters)."""
        if self.state == "success":
            return "Docs are in sync with this PR"
        if self.state == "error":
            return f"Could not finish checking the docs ({_plural(self.errors, 'error')})"
        parts = []
        if self.stale:
            parts.append(f"{_plural(self.stale, 'stale doc section')}")
        if self.failing:
            parts.append(f"{_plural(self.failing, 'failing example')}")
        return ", ".join(parts)


def summarize(proposals: DocProposals, min_confidence: float) -> CheckSummary:
    execution = proposals.execution or ExecutionReport()
    return CheckSummary(
        stale=len(change_rows(proposals, min_confidence)) + len(execution.fixed),
        failing=len(execution.failing),
        errors=len(proposals.errors),
    )


def render_premerge_comment(
    result: AnalysisResult, proposals: DocProposals, min_confidence: float, fixes: dict[str, str]
) -> str:
    """`fixes`: the verified new content of every doc file `/driftless apply` would commit."""
    pr = result.pr
    check = summarize(proposals, min_confidence)
    execution = proposals.execution
    sha = pr.head_sha[:7]
    parts = [premerge_marker(pr.number)]

    if check.state == "success":
        ran = f" {_examples(execution.total, 'pass')} against the app." if execution and execution.total else ""
        parts += [
            "## ✅ Docs are in sync with this PR",
            "",
            f"Driftless checked {_plural(len(result.candidates), 'related section')} across "
            f"{_plural(len(result.docs), 'doc file')} at `{sha}`: nothing is stale and no new interface "
            f"is undocumented.{ran}",
        ]
    elif check.stale:
        parts += [
            f"## ⚠️ This PR will make {_plural(check.stale, 'doc section')} stale",
            "",
            f"Driftless compared the docs with the code at `{sha}`. Fix them before merging:",
        ]
    elif check.failing:
        parts += [f"## ❌ {_examples(check.failing, 'fail', every=False)} against this PR", ""]
    else:
        parts += ["## ⚠️ Driftless could not finish checking the docs", ""]

    parts += _stale_table(proposals, min_confidence)
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


def _stale_table(proposals: DocProposals, min_confidence: float) -> list[str]:
    rows = change_rows(proposals, min_confidence)
    fixed = proposals.execution.fixed if proposals.execution else []
    if not rows and not fixed:
        return []
    lines = ["", "| File | Section | Why | Proposed fix |", "|------|---------|-----|--------------|"]
    lines += [
        f"| `{r.file}` | {_cell(r.section)} | {_cell(_first_sentence(r.why))} | {r.change}: {_cell(status_label(r.verification))} |"
        for r in rows
    ]
    lines += [
        f"| `{o.file}` | example `{_cell(o.label)}` (line {o.line}) | fails when run: {_cell('; '.join(o.failures))} "
        "| 🔁 fixed after running |"
        for o in fixed
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
