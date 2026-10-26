import json

from docdrift.analyzer import Analyzer
from docdrift.models import ChangedFile, DocFile, PullRequestInfo, SourceFile
from docdrift.pipeline import analyze
from docdrift.proposals import (
    BatchSectionVerdict,
    BatchVerdicts,
    NewSectionProposal,
    SectionVerdict,
    VerificationStatus,
)
from docdrift.reviewer import batch_candidates

README = """\
# Notes

## Config

Set `MAX_NOTE_LENGTH`.

## Endpoints

### `GET /notes`

List.

### `DELETE /notes/{id}`

Delete.

## Service functions (`app/service.py`)

### `list_notes(limit=20)`
Lists notes.
"""
API = "# API\n\n## List notes\n\n`GET /notes` lists notes.\n\n## Delete a note\n\n`DELETE /notes/{id}`\n"

BEFORE = (
    'import os\nMAX = os.getenv("MAX_NOTE_LENGTH")\n\n@app.get("/notes")\ndef list_notes(limit: int = 20):\n'
    "    return []\n\n@app.delete(\"/notes/{id}\")\ndef delete(id: int):\n    return id\n"
)
AFTER = (
    'import os\nMAX = os.getenv("MAX_BODY_LENGTH")\nPAGE = os.getenv("PAGE_SIZE")\n\n'
    '@app.get("/notes")\ndef list_notes(limit: int = 20, offset: int = 0):\n    return []\n\n'
    '@app.delete("/notes/{id}")\ndef delete(id: int):\n    return id\n\n'
    '@app.patch("/notes/{id}")\ndef update(id: int):\n    return id\n'
)


def make_result():
    pr = PullRequestInfo(
        repo="me/notes", number=1, title="t", base_ref="main", merge_base_sha="a", head_sha="b",
        merge_commit_sha="c",
        files=[ChangedFile(path="app/main.py", before=BEFORE, after=AFTER, patch="@@ -1 +1 @@\n-MAX_NOTE_LENGTH\n+MAX_BODY_LENGTH")],
    )
    docs = [DocFile(path="README.md", content=README), DocFile(path="docs/api.md", content=API)]
    return analyze(pr, docs, sources=[SourceFile(path="app/main.py", content=AFTER)])


class ScriptedLLM:
    """Replies by prompt content; `bad_first` makes the first list_notes rewrite wrong."""

    def __init__(self, bad_first: bool = True, bad_always: bool = False) -> None:
        self.prompts: list[str] = []
        self.bad_first = bad_first
        self.bad_always = bad_always

    def complete(self, system, user, schema, check=None, output_tokens=0):
        self.prompts.append(user)
        if schema is BatchVerdicts:
            reply = BatchVerdicts(sections=[self._verdict(i, s) for i, s in _sections(user)])
        elif schema is SectionVerdict:  # verification retry
            reply = self._rewrite_list_notes(retry=True)
        else:
            reply = self._new_section(user)
        if check:
            check(reply)
        return reply

    def _verdict(self, id_, text):
        if "MAX_NOTE_LENGTH" in text:
            updated = text.replace("MAX_NOTE_LENGTH", "MAX_BODY_LENGTH") + "\nAlso `PAGE_SIZE`."
            return BatchSectionVerdict(id=id_, stale=True, confidence=0.9, reason="renamed", updated_section=updated)
        if "list_notes(limit=20)" in text:
            return BatchSectionVerdict(id=id_, **self._rewrite_list_notes(retry=False).model_dump())
        return BatchSectionVerdict(id=id_, stale=False, confidence=0.8, reason="ok")

    def _rewrite_list_notes(self, retry: bool) -> SectionVerdict:
        wrong = self.bad_always or (self.bad_first and not retry)
        default = "PAGE_SIZE" if wrong else "20"
        return SectionVerdict(
            stale=True, confidence=0.9, reason="offset added",
            updated_section=f"### `list_notes(limit={default}, offset=0)`\nLists notes.",
        )

    def _new_section(self, user: str) -> NewSectionProposal:
        target = "docs/api.md" if 'must be exactly "docs/api.md"' in user else "README.md"
        after = "Delete a note" if target == "docs/api.md" else "`GET /notes`"
        return NewSectionProposal(
            file=target, after_heading=after, section="### `PATCH /notes/{id}`\n\nUpdate a note.",
            reason="new route", confidence=0.9,
        )


def _sections(prompt: str) -> list[tuple[str, str]]:
    parts = prompt.split('<section id="')[1:]
    return [(p.split('"', 1)[0], p.split(">\n", 1)[1].split("\n</section>", 1)[0]) for p in parts]


def test_batches_group_sections_per_file() -> None:
    result = make_result()
    batches = batch_candidates(result.candidates)
    assert [{c.section.path for c in b} for b in batches] == [{"README.md"}, {"docs/api.md"}]


def test_verified_fixed_on_retry_and_missing_docs_in_every_covering_file() -> None:
    result = make_result()
    llm = ScriptedLLM()
    proposals = Analyzer(llm, workers=1).propose(result)  # type: ignore[arg-type]

    statuses = {f.section.heading: f.verification.status for f in proposals.findings if f.verification}
    assert statuses == {"Config": VerificationStatus.VERIFIED, "`list_notes(limit=20)`": VerificationStatus.FIXED_ON_RETRY}
    fixed = next(f for f in proposals.findings if f.section.heading == "`list_notes(limit=20)`")
    assert fixed.verification.failures == [
        "list_notes: default of limit is 20 in code, doc says PAGE_SIZE (app/main.py)"
    ]
    assert "list_notes(limit=20, offset=0)" in (fixed.verdict.updated_section or "")
    retry_prompt = next(p for p in llm.prompts if "Verification failures" in p)
    assert "- list_notes: default of limit is 20 in code, doc says PAGE_SIZE (app/main.py)" in retry_prompt

    # The PATCH route goes into both files that document routes; env vars are covered by the
    # Config rewrite.
    assert sorted(i.proposal.file for i in proposals.insertions) == ["README.md", "docs/api.md"]
    assert all(i.verification.status == VerificationStatus.VERIFIED for i in proposals.insertions)
    assert {(c.item.name, c.file) for c in proposals.covered} == {
        ("MAX_BODY_LENGTH", "README.md"),
        ("PAGE_SIZE", "README.md"),
    }
    assert proposals.errors == []


def test_change_still_failing_after_retry_is_dropped() -> None:
    proposals = Analyzer(ScriptedLLM(bad_always=True), workers=1).propose(make_result())  # type: ignore[arg-type]
    dropped = next(f for f in proposals.findings if f.section.heading == "`list_notes(limit=20)`")
    assert dropped.verification.status == VerificationStatus.DROPPED
    assert dropped.verification.retry_failures == [
        "list_notes: default of limit is 20 in code, doc says PAGE_SIZE (app/main.py)"
    ]


def test_batch_prompt_has_heading_paths_and_only_relevant_hunks() -> None:
    llm = ScriptedLLM()
    Analyzer(llm, workers=1).propose(make_result())  # type: ignore[arg-type]
    batch_prompt = next(p for p in llm.prompts if "README.md" in p and '<section id="s1"' in p)
    assert 'heading_path="Notes > Config"' in batch_prompt
    assert json.dumps("MAX_NOTE_LENGTH")[1:-1] in batch_prompt
