from docdrift.analyzer import (
    DocProposals,
    Insertion,
    NewSectionProposal,
    SectionVerdict,
    StaleFinding,
)
from docdrift.doc_editor import apply_edits, build_edits, unified_diff
from docdrift.doc_index import index_document
from docdrift.models import DocFile, MissingDoc

DOC = DocFile(
    path="README.md",
    content="# App\n\n## Config\n\nSet `OLD_VAR`.\n\n## Endpoints\n\n### `GET /a`\n\nGet a.\n\n## Layout\n\nFiles.\n",
)


def finding(heading: str, updated: str | None, confidence: float = 0.9) -> StaleFinding:
    section = next(s for s in index_document(DOC) if s.heading == heading)
    verdict = SectionVerdict(stale=updated is not None, confidence=confidence, reason="r", updated_section=updated)
    return StaleFinding(section=section, matched_terms=[], verdict=verdict)


def insertion(after: str | None, confidence: float = 0.9) -> Insertion:
    proposal = NewSectionProposal(
        file="README.md", after_heading=after, section="### `PATCH /a`\n\nPatch a.", reason="r", confidence=confidence
    )
    return Insertion(item=MissingDoc(kind="route", name="PATCH /a", file="main.py"), proposal=proposal)


def apply(proposals: DocProposals, min_confidence: float = 0.5) -> str:
    sections = index_document(DOC)
    edits = build_edits([DOC], sections, proposals, min_confidence)
    return apply_edits([DOC], edits).get(DOC.path, DOC.content)


def test_replacement_keeps_separating_blank_line() -> None:
    new = apply(DocProposals(findings=[finding("Config", "## Config\n\nSet `NEW_VAR`.")]))
    assert "## Config\n\nSet `NEW_VAR`.\n\n## Endpoints" in new


def test_insert_after_heading_goes_after_its_subsections() -> None:
    new = apply(DocProposals(insertions=[insertion("Endpoints")]))
    assert "Get a.\n\n### `PATCH /a`\n\nPatch a.\n\n## Layout" in new


def test_heading_match_ignores_backticks_and_hashes() -> None:
    new = apply(DocProposals(insertions=[insertion("## GET /a")]))
    assert "Get a.\n\n### `PATCH /a`" in new


def test_insert_at_end_of_file() -> None:
    assert apply(DocProposals(insertions=[insertion(None)])).endswith("Files.\n\n### `PATCH /a`\n\nPatch a.\n")


def test_replacement_and_insertion_at_same_boundary() -> None:
    proposals = DocProposals(
        findings=[finding("`GET /a`", "### `GET /a`\n\nGet a, now faster.")],
        insertions=[insertion("Endpoints")],
    )
    new = apply(proposals)
    assert "Get a, now faster.\n\n### `PATCH /a`\n\nPatch a.\n\n## Layout" in new


def test_low_confidence_and_non_stale_are_ignored() -> None:
    proposals = DocProposals(
        findings=[finding("Config", "## Config\n\nchanged", confidence=0.2), finding("Layout", None)],
        insertions=[insertion("Endpoints", confidence=0.1)],
    )
    assert apply(proposals) == DOC.content


def test_unified_diff_format() -> None:
    diff = unified_diff("README.md", "a\nb\n", "a\nc\n")
    assert diff.startswith("--- a/README.md\n+++ b/README.md\n")
    assert "-b\n+c\n" in diff
