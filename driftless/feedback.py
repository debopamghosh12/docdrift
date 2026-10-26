"""Between the memory and a run: what a docs PR proposed, what the model is told, the 🧠 line."""

import difflib

from driftless.memory import RememberedChange, RepoMemory
from driftless.pipeline import AnalysisResult
from driftless.proposals import DocProposals

MAX_DIFF_CHARS = 1200
MAX_NOTE_CHARS = 300


def proposed_changes(
    result: AnalysisResult, proposals: DocProposals, min_confidence: float, files: list[str]
) -> list[RememberedChange]:
    """The changes a docs PR commits to `files`: rewrites, new sections and examples fixed after running."""
    changes: list[RememberedChange] = []
    for f in proposals.findings:
        v = f.verdict
        if not (v.stale and v.confidence >= min_confidence and f.verification and f.verification.applied):
            continue
        if f.section.path in files:
            changes.append(
                RememberedChange(
                    file=f.section.path, section=f.section.heading, kind="updated", reason=v.reason,
                    diff=section_diff(f.section.text, v.updated_section or ""),
                )
            )
    for ins in proposals.insertions:
        p = ins.proposal
        if p.confidence >= min_confidence and ins.verification and ins.verification.applied and p.file in files:
            heading = p.section.strip().splitlines()[0].lstrip("#").strip()
            changes.append(
                RememberedChange(
                    file=p.file, section=heading, kind="added", reason=p.reason, diff=section_diff("", p.section)
                )
            )
    if proposals.execution:
        changes += [
            RememberedChange(file=o.file, section=o.label, kind="example", reason="; ".join(o.fixes))
            for o in proposals.execution.fixed
            if o.file in files
        ]
    return changes


def section_diff(before: str, after: str) -> str:
    lines = difflib.unified_diff(before.strip("\n").splitlines(), after.strip("\n").splitlines(), lineterm="", n=1)
    diff = "\n".join(line for line in lines if not line.startswith(("---", "+++")))
    return diff if len(diff) <= MAX_DIFF_CHARS else diff[:MAX_DIFF_CHARS] + "\n..."


def memory_prompt(memory: RepoMemory) -> str:
    """The part of a review prompt that carries the maintainers' preferences and rejections."""
    parts: list[str] = []
    if memory.style:
        parts += ["## Maintainer style preferences for this repo", "Follow these in everything you write:"]
        parts += [f"- {_clip(note)}" for note in memory.style]
        parts.append("")
    if memory.rejected:
        parts += [
            "## Changes the maintainers rejected before",
            "They closed Driftless docs PRs containing these changes without merging them. Do not propose "
            "the same kind of change again unless this code change clearly requires it.",
        ]
        for change in memory.rejected:
            where = f"{change.file} › {change.section}" + (f" (docs PR #{change.docs_pr})" if change.docs_pr else "")
            parts.append(f"- {where}: {_clip(change.reason)}")
            if change.diff:
                parts += ["```diff", change.diff, "```"]
        parts.append("")
    return "\n".join(parts)


def remembered_line(result: AnalysisResult, proposals: DocProposals) -> str | None:
    """'🧠 Remembered for this repo: ...' when an ignore rule matched or the model got guidance."""
    parts: list[str] = []
    if result.ignored:
        rules = ", ".join(f"`{rule}`" for rule in result.ignored)
        parts.append(f"applied {_plural(len(result.ignored), 'ignore rule')} ({rules})")
    if proposals.guided_by_memory:
        if result.memory.style:
            parts.append(f"followed {_plural(len(result.memory.style), 'style preference')}")
        if result.memory.rejected:
            parts.append(f"showed the model {_plural(len(result.memory.rejected), 'change')} rejected before")
    if not parts:
        return None
    return f"🧠 Remembered for this repo: {' · '.join(parts)}. Reply `/driftless memory` to see everything."


def _clip(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= MAX_NOTE_CHARS else text[: MAX_NOTE_CHARS - 1] + "…"


def _plural(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"
