from driftless.doc_editor import apply_proposals, insert_section, unified_diff
from driftless.doc_index import index_document
from driftless.models import DocFile, MissingDoc
from driftless.proposals import (
    DocProposals,
    Insertion,
    NewSectionProposal,
    SectionVerdict,
    StaleFinding,
    Verification,
    VerificationStatus,
)

VERIFIED = Verification(status=VerificationStatus.VERIFIED)

DOC = DocFile(
    path="README.md",
    content="# App\n\n## Config\n\nSet `OLD_VAR`.\n\n## Endpoints\n\n### `GET /a`\n\nGet a.\n\n## Layout\n\nFiles.\n",
)


def finding(heading: str, updated: str | None, confidence: float = 0.9) -> StaleFinding:
    section = next(s for s in index_document(DOC) if s.heading == heading)
    verdict = SectionVerdict(stale=updated is not None, confidence=confidence, reason="r", updated_section=updated)
    return StaleFinding(section=section, matched_terms=[], verdict=verdict, verification=VERIFIED)


def insertion(after: str | None, confidence: float = 0.9) -> Insertion:
    proposal = NewSectionProposal(
        file="README.md", after_heading=after, section="### `PATCH /a`\n\nPatch a.", reason="r", confidence=confidence
    )
    item = MissingDoc(kind="route", name="PATCH /a", file="main.py")
    return Insertion(item=item, proposal=proposal, verification=VERIFIED)


def apply(proposals: DocProposals, min_confidence: float = 0.5) -> str:
    return apply_proposals([DOC], proposals, min_confidence).get(DOC.path, DOC.content)


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


def test_dropped_and_unverified_changes_are_not_applied() -> None:
    dropped = finding("Config", "## Config\n\nchanged")
    dropped.verification = Verification(status=VerificationStatus.DROPPED, failures=["x"])
    unverified = insertion("Endpoints")
    unverified.verification = None
    assert apply(DocProposals(findings=[dropped], insertions=[unverified])) == DOC.content


def test_unified_diff_format() -> None:
    diff = unified_diff("README.md", "a\nb\n", "a\nc\n")
    assert diff.startswith("--- a/README.md\n+++ b/README.md\n")
    assert "-b\n+c\n" in diff


def test_insert_goes_before_trailing_cross_reference_lines() -> None:
    content = "## Endpoints\n\n### `DELETE /a`\n\nDelete a.\n\nSee docs/api.md for details.\n\n## Layout\n"
    new = insert_section("README.md", content, "DELETE /a", "### `PATCH /a`\n\nPatch a.")
    assert new == (
        "## Endpoints\n\n### `DELETE /a`\n\nDelete a.\n\n### `PATCH /a`\n\nPatch a.\n\n"
        "See docs/api.md for details.\n\n## Layout\n"
    )


def test_insert_matches_thematic_break_separators() -> None:
    content = "# API\n\n## Get\n\nGet it.\n\n---\n\n## Delete\n\nDelete it.\n"
    new = insert_section("docs/api.md", content, "Get", "## Update\n\nUpdate it.")
    assert new == "# API\n\n## Get\n\nGet it.\n\n---\n\n## Update\n\nUpdate it.\n\n---\n\n## Delete\n\nDelete it.\n"
    # After the last section there is no break below, so the one above its heading is copied.
    new = insert_section("docs/api.md", content, "Delete", "## Update\n\nUpdate it.")
    assert new.endswith("Delete it.\n\n---\n\n## Update\n\nUpdate it.\n")


SEPARATED = DocFile(
    path="docs/api.md",
    content="# API\n\n## Errors\n\nOld `X`.\n\n---\n\n## List\n\nList.\n\n---\n\n## Delete\n\nDelete.\n",
)


def test_rewrite_keeps_the_separator_the_model_dropped() -> None:
    # Seen in a real run: the rewritten Errors section came back without its trailing ---.
    errors = next(s for s in index_document(SEPARATED) if s.heading == "Errors")
    verdict = SectionVerdict(stale=True, confidence=0.9, reason="r", updated_section="## Errors\n\nNew `Y`.\n")
    proposals = DocProposals(findings=[StaleFinding(section=errors, matched_terms=[], verdict=verdict, verification=VERIFIED)])
    new = apply_proposals([SEPARATED], proposals, 0.5)["docs/api.md"]
    assert new == SEPARATED.content.replace("Old `X`.", "New `Y`.")


def test_new_section_with_its_own_separator_gets_only_one() -> None:
    # Seen in a real run: the model ended its new section with ---, doubling the separator.
    new = insert_section("docs/api.md", SEPARATED.content, "List", "## Update\n\nUpdate.\n\n---\n")
    assert "List.\n\n---\n\n## Update\n\nUpdate.\n\n---\n\n## Delete" in new


def test_no_separator_when_file_has_none_or_only_front_matter() -> None:
    content = "---\ntitle: x\n---\n## Get\n\nGet it.\n"
    new = insert_section("docs/a.md", content, None, "## Put\n\nPut it.")
    assert new.endswith("Get it.\n\n## Put\n\nPut it.\n")


def test_insert_after_rewritten_section_uses_its_new_text() -> None:
    rewrite = "### `GET /a`\n\nGet a.\n\nSee also b.\nAnd more lines."
    proposals = DocProposals(findings=[finding("`GET /a`", rewrite)], insertions=[insertion("GET /a")])
    assert "And more lines.\n\n### `PATCH /a`\n\nPatch a.\n\n## Layout" in apply(proposals)
