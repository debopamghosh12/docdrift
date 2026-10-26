"""Score doc sections against a ChangeSet and flag new interfaces that have no docs."""

import re
from dataclasses import dataclass
from functools import lru_cache

from docdrift.diff_parser import is_test_path
from docdrift.models import (
    Candidate,
    ChangeKind,
    ChangeSet,
    DocSection,
    MissingDoc,
    SymbolKind,
)

# Docs that mention removed/renamed/re-signed things are the most likely to be stale.
CHANGE_WEIGHTS: dict[ChangeKind, float] = {
    ChangeKind.REMOVED: 3.0,
    ChangeKind.RENAMED: 3.0,
    ChangeKind.SIGNATURE_CHANGED: 2.5,
    ChangeKind.MODIFIED: 1.0,
    ChangeKind.ADDED: 0.5,
}
INTERFACE_BOOST = 1.5  # env vars, routes and flags are user-facing, so they matter more
PRIVATE_PENALTY = 0.5
METHOD_SHORT_NAME_FACTOR = 0.6
CODE_CONTEXT_BOOST = 1.5
MIN_SCORE = 1.0

_PARAM_RE = re.compile(r"\{[^}/]+\}|:[A-Za-z_]\w*|<[^>/]+>")
_ANY_PARAM = r"(?:\{[^}/\s]+\}|:[A-Za-z_]\w*|<[^>/\s]+>|[^/\s`'\")]+)"
_CODE_SPAN_RE = re.compile(r"^ {0,3}(`{3,}|~{3,}).*?^ {0,3}\1|`[^`\n]+`", re.MULTILINE | re.DOTALL)
_GENERIC_WORD_RE = re.compile(r"[a-z]+")


@dataclass(frozen=True)
class Term:
    text: str
    kind: str  # "symbol" | "env" | "route" | "flag"
    weight: float

    @property
    def is_generic(self) -> bool:
        """Plain lowercase words (`run`, `serve`) only count when they appear in code."""
        return self.kind == "symbol" and bool(_GENERIC_WORD_RE.fullmatch(self.text))


def build_terms(changeset: ChangeSet) -> list[Term]:
    terms: list[Term] = []
    for symbol in changeset.symbols:
        factor = 1.0 if symbol.public else PRIVATE_PENALTY
        if symbol.change == ChangeKind.RENAMED and symbol.old_name:
            terms += _symbol_terms(symbol.old_name, CHANGE_WEIGHTS[ChangeKind.RENAMED] * factor)
            terms += _symbol_terms(symbol.name, CHANGE_WEIGHTS[ChangeKind.ADDED] * factor)
        else:
            terms += _symbol_terms(symbol.name, CHANGE_WEIGHTS[symbol.change] * factor)
    for env in changeset.env_vars:
        terms.append(Term(env.name, "env", CHANGE_WEIGHTS[env.change] * INTERFACE_BOOST))
    for route in changeset.routes:
        if len(route.path) > 1:
            terms.append(Term(route.path, "route", CHANGE_WEIGHTS[route.change] * INTERFACE_BOOST))
    for flag in changeset.cli_flags:
        terms.append(Term(flag.flag, "flag", CHANGE_WEIGHTS[flag.change] * INTERFACE_BOOST))
    return _dedupe(terms)


def score_section(section: DocSection, terms: list[Term]) -> tuple[float, list[str]]:
    spans = _code_spans(section.text)
    heading = " ".join(section.heading_path)
    total = 0.0
    matched: list[str] = []
    for term in terms:
        hits = [m.start() for m in _pattern(term.text, term.kind).finditer(section.text)]
        in_code = sum(1 for h in hits if any(start <= h < end for start, end in spans))
        count = in_code if term.is_generic else len(hits)
        if not count:
            continue
        score = term.weight * (1 + 0.5 * min(count - 1, 4))
        if in_code:
            score *= CODE_CONTEXT_BOOST
        if _pattern(term.text, term.kind).search(heading):
            score += term.weight
        total += score
        matched.append(term.text)
    return round(total, 2), matched


def select_candidates(
    sections: list[DocSection], changeset: ChangeSet, top_k: int = 10, min_score: float = MIN_SCORE
) -> list[Candidate]:
    terms = build_terms(changeset)
    scored = []
    for section in sections:
        score, matched = score_section(section, terms)
        if score >= min_score:
            scored.append(Candidate(section=section, score=score, matched_terms=matched))
    scored.sort(key=lambda c: (-c.score, c.section.path, c.section.start_line))
    return scored[:top_k]


def find_missing_docs(sections: list[DocSection], changeset: ChangeSet) -> list[MissingDoc]:
    """New public functions/classes, env vars, routes and flags that no section mentions."""
    items: list[tuple[MissingDoc, Term]] = []
    for symbol in changeset.symbols:
        is_new = symbol.change in (ChangeKind.ADDED, ChangeKind.RENAMED)
        top_level = symbol.kind in (SymbolKind.FUNCTION, SymbolKind.CLASS)
        if is_new and top_level and symbol.public and not is_test_path(symbol.file):
            doc = MissingDoc(kind=symbol.kind.value, name=symbol.name, file=symbol.file)
            items.append((doc, Term(symbol.name, "symbol", 1.0)))
    for env in changeset.env_vars:
        if env.change == ChangeKind.ADDED:
            items.append((MissingDoc(kind="env_var", name=env.name, file=env.file), Term(env.name, "env", 1.0)))
    for route in changeset.routes:
        if route.change == ChangeKind.ADDED and len(route.path) > 1:
            doc = MissingDoc(kind="route", name=f"{route.method} {route.path}", file=route.file)
            items.append((doc, Term(route.path, "route", 1.0)))
    for flag in changeset.cli_flags:
        if flag.change == ChangeKind.ADDED:
            items.append((MissingDoc(kind="cli_flag", name=flag.flag, file=flag.file), Term(flag.flag, "flag", 1.0)))
    return [doc for doc, term in items if not any(score_section(s, [term])[0] for s in sections)]


def _symbol_terms(qualname: str, weight: float) -> list[Term]:
    terms = [Term(qualname, "symbol", weight)]
    if "." in qualname:
        parent, short = qualname.rsplit(".", 1)
        # A constructor change affects how the class itself is documented.
        if short == "__init__":
            terms.append(Term(parent, "symbol", weight))
        else:
            terms.append(Term(short, "symbol", weight * METHOD_SHORT_NAME_FACTOR))
    return terms


def _dedupe(terms: list[Term]) -> list[Term]:
    best: dict[tuple[str, str], Term] = {}
    for term in terms:
        key = (term.text, term.kind)
        if key not in best or term.weight > best[key].weight:
            best[key] = term
    return list(best.values())


@lru_cache(maxsize=2048)
def _pattern(text: str, kind: str) -> re.Pattern[str]:
    if kind == "route":
        return re.compile(_route_regex(text))
    if kind == "flag":
        return re.compile(rf"(?<![\w-]){re.escape(text)}(?![\w-])")
    return re.compile(rf"(?<![\w$]){re.escape(text)}(?![\w$])")


def _route_regex(path: str) -> str:
    """Treat `{id}`, `:id` and `<id>` path params as interchangeable."""
    parts: list[str] = []
    pos = 0
    for m in _PARAM_RE.finditer(path):
        parts.append(re.escape(path[pos : m.start()]))
        parts.append(_ANY_PARAM)
        pos = m.end()
    parts.append(re.escape(path[pos:]))
    return "".join(parts) + r"(?![\w-])"


def _code_spans(text: str) -> list[tuple[int, int]]:
    return [m.span() for m in _CODE_SPAN_RE.finditer(text)]
