"""Apply proposed section rewrites and insertions to markdown files; produce unified diffs.

Rewrites are applied first, by the line ranges of the original sections. Insertions are then
placed one at a time against the updated text, so they land after a rewritten neighbour's
real content rather than inside a range that no longer exists.
"""

import difflib
import re
from dataclasses import dataclass

from driftless.doc_index import index_document
from driftless.models import DocFile, DocSection
from driftless.normalize import normalize_heading
from driftless.proposals import DocProposals, Verification

# "See docs/api.md", "For more details, ...", "> See also ..." closing a section.
_CROSS_REFERENCE_RE = re.compile(
    r"^\s*(?:[>*_]\s*)*(?:see(?:\s+also)?|for\s+(?:more|details|the\s+full)|refer\s+to|more\s+(?:info|details))\b",
    re.IGNORECASE,
)
_THEMATIC_BREAK_RE = re.compile(r"^ {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$")


@dataclass(frozen=True)
class Edit:
    """Replace 0-based lines [start, end) of `path` with `lines`."""

    path: str
    start: int
    end: int
    lines: list[str]


def apply_proposals(docs: list[DocFile], proposals: DocProposals, min_confidence: float) -> dict[str, str]:
    """Return the new content of every file changed by trusted, confident proposals."""
    contents = {d.path: d.content for d in docs}
    edits: list[Edit] = []
    for finding in proposals.findings:
        verdict = finding.verdict
        if verdict.stale and verdict.updated_section and verdict.confidence >= min_confidence:
            if _trusted(finding.verification) and finding.section.path in contents:
                edits.append(_replacement(finding.section, verdict.updated_section))
    for path in contents:
        contents[path] = apply_edits(contents[path], [e for e in edits if e.path == path])

    for insertion in proposals.insertions:
        proposal = insertion.proposal
        if proposal.confidence < min_confidence or not _trusted(insertion.verification):
            continue
        if proposal.file in contents:
            contents[proposal.file] = insert_section(
                proposal.file, contents[proposal.file], proposal.after_heading, proposal.section
            )
    originals = {d.path: d.content for d in docs}
    return {path: text for path, text in contents.items() if text != originals[path]}


def final_docs(docs: list[DocFile], proposals: DocProposals, min_confidence: float) -> dict[str, str]:
    """Like apply_proposals, plus fixes made to examples after running them."""
    updated = apply_proposals(docs, proposals, min_confidence)
    if proposals.execution:
        originals = {d.path: d.content for d in docs}
        for path, text in proposals.execution.contents.items():
            if path in originals and text != originals[path]:
                updated[path] = text
    return updated


def apply_edits(content: str, edits: list[Edit]) -> str:
    """Apply non-overlapping line-range edits bottom-up so earlier line numbers stay valid."""
    if not edits:
        return content
    lines = content.splitlines()
    for edit in sorted(edits, key=lambda e: e.start, reverse=True):
        lines[edit.start : edit.end] = edit.lines
    trailing = "\n" if content.endswith("\n") else ""
    return "\n".join(lines) + trailing


def insert_section(path: str, content: str, after_heading: str | None, section: str) -> str:
    """Insert `section` after `after_heading`'s block (end of file when None).

    It goes right after the block's real content: before trailing blank lines, cross-reference
    lines ("See ...") and separators. If neighbouring sections are separated by a thematic
    break (---), the new section gets one too.
    """
    lines = content.splitlines()
    sections = index_document(DocFile(path=path, content=content))
    heading_line, block_end = _anchor_block(sections, after_heading, len(lines))
    point = _content_end(lines, heading_line, block_end)
    separator = _separator(lines, heading_line, point, block_end)

    block: list[str] = [""] if point > 0 else []
    if separator:
        block += [separator, ""]
    block += _strip_structure(section.splitlines())
    if point < len(lines) and lines[point].strip():
        block.append("")
    lines[point:point] = block
    trailing = "\n" if content.endswith("\n") or not content else ""
    return "\n".join(lines) + trailing


def unified_diff(path: str, old: str, new: str) -> str:
    return "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
    )


def _trusted(verification: Verification | None) -> bool:
    """Only verified (or fixed-on-retry) changes are applied."""
    return verification is not None and verification.applied


def _replacement(section: DocSection, updated: str) -> Edit:
    # The blank lines and --- separating this section from the next are structure, not
    # content: keep the original ones (LLMs drop or duplicate them).
    original = section.text.split("\n")
    content_end = len(original)
    while content_end > 0 and _is_structural(original[content_end - 1]):
        content_end -= 1
    new_lines = _strip_structure(updated.splitlines()) + original[content_end:]
    return Edit(section.path, section.start_line - 1, section.end_line, new_lines)


def _is_structural(line: str) -> bool:
    return not line.strip() or bool(_THEMATIC_BREAK_RE.match(line))


def _strip_structure(lines: list[str]) -> list[str]:
    """Drop leading/trailing blank lines and thematic breaks."""
    start, end = 0, len(lines)
    while start < end and _is_structural(lines[start]):
        start += 1
    while end > start and _is_structural(lines[end - 1]):
        end -= 1
    return lines[start:end]


def _anchor_block(sections: list[DocSection], after_heading: str | None, end_of_file: int) -> tuple[int, int]:
    """(0-based heading line, exclusive end) of the heading's section plus its subsections."""
    if after_heading is not None:
        wanted = normalize_heading(after_heading)
        for i, section in enumerate(sections):
            if normalize_heading(section.heading) != wanted:
                continue
            last = section
            for child in sections[i + 1 :]:
                if child.level <= section.level:
                    break
                last = child
            return section.start_line - 1, last.end_line
    # End of file: the block is the file's last section.
    return (sections[-1].start_line - 1 if sections else 0), end_of_file


def _content_end(lines: list[str], heading_line: int, block_end: int) -> int:
    """Index just after the block's last line of real content."""
    point = block_end
    while point - 1 > heading_line:
        line = lines[point - 1]
        if line.strip() and not _CROSS_REFERENCE_RE.match(line) and not _THEMATIC_BREAK_RE.match(line):
            break
        point -= 1
    return point


def _separator(lines: list[str], heading_line: int, point: int, block_end: int) -> str | None:
    """The thematic break separating this block from its neighbours, if the file uses one."""
    for line in lines[point:block_end]:
        if _THEMATIC_BREAK_RE.match(line):
            return line.strip()
    # Otherwise copy the break above the block's heading (one preceded by a blank line, so
    # front matter's closing --- doesn't count).
    previous = [i for i in range(heading_line) if lines[i].strip()]
    if previous and _THEMATIC_BREAK_RE.match(lines[previous[-1]]):
        k = previous[-1]
        if k > 0 and not lines[k - 1].strip():
            return lines[k].strip()
    return None
