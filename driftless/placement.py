"""Decide which doc files should document a missing item."""

from driftless.candidates import Term, score_text
from driftless.code_index import CodeIndex
from driftless.evidence import best_example, file_anchors
from driftless.models import ChangedFile, ChangeKind, ChangeSet, DocFile, DocSection, MissingDoc

MIN_SIBLINGS_MENTIONED = 2
LISTED_KINDS = frozenset({"env_var", "cli_flag"})


def covering_files(
    item: MissingDoc,
    docs: list[DocFile],
    sections: list[DocSection],
    code: CodeIndex,
    changeset: ChangeSet,
    pr_files: list[ChangedFile],
) -> list[str]:
    """Every doc file that already documents things of the same kind as `item`.

    A file covers routes if it mentions other routes, env vars if it mentions other env vars,
    a function if it mentions its siblings from the same source file. Items of the same kind
    removed by this PR count too: the file that documented them is where the kind lives.
    """
    terms = sibling_terms(item, code, changeset, pr_files)
    if not terms:
        return []
    needed = min(MIN_SIBLINGS_MENTIONED, len(terms))

    def mentions(text: str) -> int:
        return sum(1 for term in terms if score_text(text, "", [term])[0] > 0)

    if item.kind in LISTED_KINDS:
        # Env vars and flags are documented together in one table/list; a passing mention of
        # one in some other section doesn't make a file their reference.
        return sorted({s.path for s in sections if mentions(s.text) >= needed})
    return [doc.path for doc in docs if mentions(doc.content) >= needed]


def fallback_file(item: MissingDoc, sections: list[DocSection], pr_files: list[ChangedFile]) -> str | None:
    """The file holding the section most related to the item's source file."""
    changed = next((f for f in pr_files if f.path == item.file), None)
    example = best_example(sections, item.name, file_anchors(changed) - {item.name})
    if example:
        return example.path
    return sections[0].path if sections else None


def sibling_terms(
    item: MissingDoc, code: CodeIndex, changeset: ChangeSet, pr_files: list[ChangedFile]
) -> list[Term]:
    removed = ChangeKind.REMOVED
    if item.kind == "route":
        labels = {f"{m} {p}" for m, p in code.routes} | {r.label for r in changeset.routes if r.change == removed}
        return [Term(label, "route", 1.0) for label in sorted(labels - {item.name})]
    if item.kind == "env_var":
        names = code.env_vars | {e.name for e in changeset.env_vars if e.change == removed}
        return [Term(name, "env", 1.0) for name in sorted(names - {item.name})]
    if item.kind == "cli_flag":
        flags = code.cli_flags | {f.flag for f in changeset.cli_flags if f.change == removed}
        return [Term(flag, "flag", 1.0) for flag in sorted(flags - {item.name})]
    changed = next((f for f in pr_files if f.path == item.file), None)
    anchors = file_anchors(changed) - {item.name, item.file}
    removed_here = {s.name for s in changeset.symbols if s.change == removed and s.file == item.file}
    return [Term(a, "symbol", 1.0) for a in sorted(anchors | removed_here) if " " not in a]
