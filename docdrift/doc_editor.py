"""Apply proposed section rewrites and insertions to markdown files; produce unified diffs."""

import difflib
from dataclasses import dataclass

from docdrift.analyzer import DocProposals, normalize_heading
from docdrift.models import DocFile, DocSection


@dataclass(frozen=True)
class Edit:
    """Replace 0-based lines [start, end) of `path` with `lines` (start == end inserts)."""

    path: str
    start: int
    end: int
    lines: list[str]


def build_edits(
    docs: list[DocFile], sections: list[DocSection], proposals: DocProposals, min_confidence: float
) -> list[Edit]:
    contents = {d.path: d.content.splitlines() for d in docs}
    edits: list[Edit] = []
    for finding in proposals.findings:
        verdict = finding.verdict
        if verdict.stale and verdict.updated_section and verdict.confidence >= min_confidence:
            edits.append(_replacement(finding.section, verdict.updated_section))
    for insertion in proposals.insertions:
        proposal = insertion.proposal
        if proposal.confidence < min_confidence:
            continue
        file_sections = [s for s in sections if s.path == proposal.file]
        lines = contents[proposal.file]
        point = _insertion_point(file_sections, proposal.after_heading, len(lines))
        new_lines = proposal.section.strip("\n").splitlines()
        if point < len(lines):
            new_lines.append("")
        if point > 0 and lines[point - 1].strip():
            new_lines = [""] + new_lines
        edits.append(Edit(proposal.file, point, point, new_lines))
    return edits


def apply_edits(docs: list[DocFile], edits: list[Edit]) -> dict[str, str]:
    """Return the new content of every file that changed."""
    updated: dict[str, str] = {}
    for doc in docs:
        file_edits = [e for e in edits if e.path == doc.path]
        if not file_edits:
            continue
        lines = doc.content.splitlines()
        # Bottom-up keeps earlier line numbers valid; at equal starts, inserts go before replacements.
        for edit in sorted(file_edits, key=lambda e: (e.start, e.end == e.start), reverse=True):
            lines[edit.start : edit.end] = edit.lines
        trailing = "\n" if doc.content.endswith("\n") else ""
        new_content = "\n".join(lines) + trailing
        if new_content != doc.content:
            updated[doc.path] = new_content
    return updated


def unified_diff(path: str, old: str, new: str) -> str:
    return "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
    )


def _replacement(section: DocSection, updated: str) -> Edit:
    # Keep the blank lines that separated this section from the next one (LLMs strip them).
    total_lines = section.end_line - section.start_line + 1
    trailing_blank = total_lines - len(section.text.rstrip().splitlines())
    new_lines = updated.strip("\n").splitlines() + [""] * trailing_blank
    return Edit(section.path, section.start_line - 1, section.end_line, new_lines)


def _insertion_point(sections: list[DocSection], after_heading: str | None, end_of_file: int) -> int:
    if after_heading is None:
        return end_of_file
    wanted = normalize_heading(after_heading)
    for i, section in enumerate(sections):
        if normalize_heading(section.heading) != wanted:
            continue
        last = section
        for child in sections[i + 1 :]:
            if child.level <= section.level:
                break
            last = child
        return last.end_line
    return end_of_file
