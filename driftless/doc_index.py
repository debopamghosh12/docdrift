"""Index markdown docs (README.md, docs/**/*.md) into heading-delimited sections."""

import re
from pathlib import PurePosixPath

from driftless.models import DocFile, DocSection

_ATX_RE = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.+?)(?:[ \t]+#+)?[ \t]*$")
_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_SETEXT_RE = re.compile(r"^ {0,3}(=+|-+)[ \t]*$")
_NOT_PARAGRAPH_RE = re.compile(r"^\s*([-*+>|]|\d+[.)]\s|$)")
PREAMBLE = "(preamble)"


def is_doc_path(path: str) -> bool:
    p = PurePosixPath(path)
    is_root_readme = len(p.parts) == 1 and p.name.lower() == "readme.md"
    return is_root_readme or (p.parts[0] == "docs" and p.suffix.lower() == ".md")


def index_documents(docs: list[DocFile]) -> list[DocSection]:
    return [section for doc in docs for section in index_document(doc)]


def index_document(doc: DocFile) -> list[DocSection]:
    lines = doc.content.splitlines()
    headings = _find_headings(lines)
    sections: list[DocSection] = []

    first_heading = headings[0][0] if headings else len(lines)
    if any(line.strip() for line in lines[:first_heading]):
        sections.append(_section(doc.path, PREAMBLE, [], 0, 0, first_heading, lines))

    stack: list[tuple[int, str]] = []
    for i, (start, level, title) in enumerate(headings):
        end = headings[i + 1][0] if i + 1 < len(headings) else len(lines)
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, title))
        sections.append(_section(doc.path, title, [t for _, t in stack], level, start, end, lines))
    return sections


def _find_headings(lines: list[str]) -> list[tuple[int, int, str]]:
    """Return (0-based line index, level, title) for ATX and setext headings outside code fences."""
    headings: list[tuple[int, int, str]] = []
    fence: str | None = None
    for i in range(_front_matter_end(lines), len(lines)):
        line = lines[i]
        if m := _FENCE_RE.match(line):
            marker = m.group(1)
            if fence is None:
                fence = marker
            elif marker[0] == fence[0] and len(marker) >= len(fence):
                fence = None
            continue
        if fence is not None:
            continue
        if m := _ATX_RE.match(line):
            headings.append((i, len(m.group(1)), m.group(2).strip()))
        elif (m := _SETEXT_RE.match(line)) and i > 0 and _is_setext_title(lines, i - 1, headings):
            level = 1 if m.group(1)[0] == "=" else 2
            headings.append((i - 1, level, lines[i - 1].strip()))
    return headings


def _is_setext_title(lines: list[str], idx: int, headings: list[tuple[int, int, str]]) -> bool:
    line = lines[idx]
    if headings and headings[-1][0] == idx:
        return False
    return not (_NOT_PARAGRAPH_RE.match(line) or _ATX_RE.match(line) or _FENCE_RE.match(line))


def _front_matter_end(lines: list[str]) -> int:
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() in ("---", "..."):
                return i + 1
    return 0


def _section(
    path: str, heading: str, heading_path: list[str], level: int, start: int, end: int, lines: list[str]
) -> DocSection:
    return DocSection(
        path=path,
        heading=heading,
        heading_path=heading_path,
        level=level,
        start_line=start + 1,
        end_line=end,
        text="\n".join(lines[start:end]),
    )
