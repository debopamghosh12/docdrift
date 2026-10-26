"""Build the evidence an LLM sees: change summary, relevant hunks, doc outline, examples."""

import re

from driftless.diff_parser.patch import reconstruct, split_hunks
from driftless.diff_parser.patterns import extract_env_vars, extract_routes
from driftless.diff_parser.python_symbols import (
    extract_python_routes,
    extract_python_symbols,
    parse_python,
)
from driftless.diff_parser.regex_symbols import extract_js_symbols
from driftless.diff_parser.symbols import is_public
from driftless.doc_index import PREAMBLE
from driftless.models import ChangedFile, ChangeKind, ChangeSet, DocSection

HUNK_BUDGET_CHARS = 8000
_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{3,}|/[\w/{}:-]+")


def describe_changeset(changeset: ChangeSet) -> str:
    lines: list[str] = []
    for s in changeset.symbols:
        where = f"{s.kind.value} in {s.file}"
        if s.change == ChangeKind.RENAMED:
            lines.append(f"- renamed {where}: {s.old_signature} -> {s.new_signature}")
        elif s.change == ChangeKind.SIGNATURE_CHANGED:
            lines.append(f"- signature changed, {where}: {s.old_signature} -> {s.new_signature}")
        else:
            lines.append(f"- {s.change.value} {where}: {s.new_signature or s.old_signature}")
    for env in changeset.env_vars:
        lines.append(f"- env var {env.name} {env.change.value} ({env.file})")
    for route in changeset.routes:
        detail = f" (was {route.old_label})" if route.old_label else ""
        if route.change == ChangeKind.MODIFIED and route.handler and not detail:
            detail = f" (handler {route.handler} changed)"
        lines.append(f"- route {route.label} {route.change.value}{detail} ({route.file})")
    for flag in changeset.cli_flags:
        lines.append(f"- CLI flag {flag.flag} {flag.change.value} ({flag.file})")
    return "\n".join(lines) or "(no structured changes detected)"


def relevant_hunks(files: list[ChangedFile], terms: list[str], budget: int = HUNK_BUDGET_CHARS) -> str:
    """Diff hunks that mention any term (route terms match on their path); all hunks if none do."""
    needles = [term.split(" ", 1)[-1] for term in terms]
    all_hunks = [(f.path, h) for f in files for h in split_hunks(f.patch)]
    matching = [(p, h) for p, h in all_hunks if any(n in h for n in needles)]
    return _format_hunks(matching or all_hunks, budget)


def doc_outline(sections: list[DocSection]) -> str:
    lines: list[str] = []
    current = ""
    for section in sections:
        if section.path != current:
            current = section.path
            lines.append(f"{current}:")
        if section.heading != PREAMBLE:
            lines.append(f"  {'  ' * (section.level - 1)}{'#' * section.level} {section.heading}")
    return "\n".join(lines)


def file_anchors(changed: ChangedFile | None) -> set[str]:
    """Names that identify docs about a file: its path, top-level symbols, routes and env vars."""
    if changed is None:
        return set()
    source = changed.after or reconstruct(changed.patch)[1]
    anchors = {changed.path} | extract_env_vars(source)
    tree = parse_python(source) if changed.path.endswith(".py") else None
    if tree is not None:
        symbols = extract_python_symbols(tree)
        anchors |= {f"{method} {path}" for method, path in extract_python_routes(tree)}
    else:
        symbols = extract_js_symbols(source)
        anchors |= {f"{method} {path}" for method, path in extract_routes(source)}
    anchors |= {name for name in symbols if "." not in name and is_public(name)}
    return anchors


def best_example(
    sections: list[DocSection], evidence: str, anchors: set[str] | None = None
) -> DocSection | None:
    """The section most related to a change: a style and placement hint for new docs.

    Sibling anchors (other names from the same file) weigh 3x, so a new service function lands
    next to the other service functions rather than next to an endpoint that calls it.
    """
    anchors = anchors or set()
    identifiers = set(_IDENTIFIER_RE.findall(evidence)) - anchors

    def score(section: DocSection) -> int:
        context = section.text + " " + " ".join(section.heading_path)
        return 3 * sum(1 for a in anchors if a in context) + sum(1 for i in identifiers if i in context)

    scored = [(score(s), -s.start_line, s) for s in sections if s.heading != PREAMBLE]
    if not scored:
        return None
    best, _, section = max(scored, key=lambda item: (item[0], item[1]))
    return section if best else None


def _format_hunks(hunks: list[tuple[str, str]], budget: int) -> str:
    parts: list[str] = []
    used = 0
    for path, hunk in hunks:
        block = f"### {path}\n```diff\n{hunk}\n```"
        if used + len(block) > budget:
            parts.append("(further hunks omitted)")
            break
        parts.append(block)
        used += len(block)
    return "\n\n".join(parts) or "(no diff available)"
