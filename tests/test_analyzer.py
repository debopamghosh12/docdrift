import json

from docdrift.analyzer import Analyzer, NewSectionProposal, SectionVerdict
from docdrift.models import ChangedFile, DocFile, PullRequestInfo
from docdrift.pipeline import analyze

README = "# Notes\n\n## Config\n\nSet `MAX_NOTE_LENGTH`.\n\n## Endpoints\n\n### `GET /notes`\n\nList.\n"


class ScriptedLLM:
    """Answers stale-section prompts by section text and missing-doc prompts by item name."""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def complete(self, system, user, schema, check=None):
        self.prompts.append(user)
        if schema is SectionVerdict:
            if "MAX_NOTE_LENGTH`." in user:
                return SectionVerdict(
                    stale=True, confidence=0.9, reason="renamed",
                    updated_section="## Config\n\nSet `MAX_BODY_LENGTH` and `PAGE_SIZE`.",
                )
            return SectionVerdict(stale=False, confidence=0.8, reason="ok")
        proposal = NewSectionProposal(
            file="README.md", after_heading="`GET /notes`", section="### `PATCH /notes/{id}`\n\nUpdate.",
            reason="new route", confidence=0.9,
        )
        if check:
            check(proposal)
        return proposal


def make_result():
    before = 'import os\nMAX = os.getenv("MAX_NOTE_LENGTH")\n\n@app.get("/notes")\ndef list_notes():\n    return []\n'
    after = (
        'import os\nMAX = os.getenv("MAX_BODY_LENGTH")\nPAGE = os.getenv("PAGE_SIZE")\n\n'
        '@app.get("/notes")\ndef list_notes():\n    return []\n\n'
        '@app.patch("/notes/{id}")\ndef update(id: int):\n    return id\n'
    )
    pr = PullRequestInfo(
        repo="me/notes", number=1, title="t", base_ref="main",
        merge_base_sha="a", head_sha="b", merge_commit_sha="c",
        files=[ChangedFile(path="app.py", before=before, after=after, patch="@@ -1 +1 @@\n-MAX_NOTE_LENGTH\n+MAX_BODY_LENGTH")],
    )
    return analyze(pr, [DocFile(path="README.md", content=README)])


def test_analyzer_reviews_candidates_and_skips_covered_missing_items() -> None:
    result = make_result()
    assert {m.name for m in result.missing} == {"MAX_BODY_LENGTH", "PAGE_SIZE", "PATCH /notes/{id}"}

    llm = ScriptedLLM()
    proposals = Analyzer(llm, workers=1).propose(result)  # type: ignore[arg-type]

    assert [f.verdict.stale for f in proposals.findings] == [True]
    # Both env vars appear in the rewritten Config section, so only the route needs a new section.
    assert {m.name for m in proposals.covered} == {"MAX_BODY_LENGTH", "PAGE_SIZE"}
    assert [i.item.name for i in proposals.insertions] == ["PATCH /notes/{id}"]
    assert proposals.errors == []
    stale_prompt = llm.prompts[0]
    assert "Heading path: Notes > Config" in stale_prompt
    assert json.dumps("MAX_NOTE_LENGTH")[1:-1] in stale_prompt


def test_unchanged_rewrite_is_not_stale() -> None:
    class EchoLLM(ScriptedLLM):
        def complete(self, system, user, schema, check=None):
            if schema is SectionVerdict:
                section = user.split("<section>\n", 1)[1].split("\n</section>", 1)[0]
                return SectionVerdict(stale=True, confidence=0.9, reason="r", updated_section=section)
            return super().complete(system, user, schema, check)

    proposals = Analyzer(EchoLLM(), workers=1).propose(make_result())  # type: ignore[arg-type]
    assert all(not f.verdict.stale for f in proposals.findings)
