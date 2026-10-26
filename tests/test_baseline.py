import sys

from driftless.baseline import mark_pre_existing, needs_baseline
from driftless.code_index import build_code_index
from driftless.doc_index import index_document
from driftless.executable_docs import execute_docs
from driftless.execution import ExampleOutcome, ExampleStatus, ExecutionReport
from driftless.models import DocFile, SourceFile
from driftless.premerge import PreMergeChecker
from driftless.premerge_body import summarize
from driftless.proposals import DocProposals, SectionVerdict, StaleFinding, Verification, VerificationStatus
from tests.test_premerge import FakeCheckRepo, open_pr
from tests.test_sandbox import fixture_files


def outcome(label: str, status: ExampleStatus, expected: int = 200, line: int = 5) -> ExampleOutcome:
    return ExampleOutcome(
        file="README.md", label=label, line=line, status=status, expected_status=expected,
        failures=[] if status == ExampleStatus.PASSED else ["status differs"],
    )


def test_examples_already_failing_at_base_are_pre_existing() -> None:
    head = ExecutionReport(outcomes=[
        outcome("POST /items", ExampleStatus.FAILING),
        outcome("GET /items/1", ExampleStatus.FIXED),
        outcome("GET /items/1", ExampleStatus.FAILING, line=30),  # 2nd occurrence: passed at base
        outcome("DELETE /items/1", ExampleStatus.FAILING, expected=204),  # the PR edited its documented status
    ])
    base = ExecutionReport(outcomes=[
        outcome("POST /items", ExampleStatus.FAILING),
        outcome("GET /items/1", ExampleStatus.FAILING),
        outcome("GET /items/1", ExampleStatus.PASSED, line=30),
        outcome("DELETE /items/1", ExampleStatus.FAILING, expected=200),
    ])
    proposals = DocProposals(execution=head)
    mark_pre_existing(proposals, build_code_index([]), [], None, base)
    assert [o.pre_existing for o in head.outcomes] == [True, True, False, False]

    check = summarize(proposals, 0.5)
    assert (check.stale, check.broken, check.pre_existing) == (0, 2, 2)


def test_only_pre_existing_drift_keeps_the_status_green() -> None:
    head = ExecutionReport(outcomes=[outcome("POST /items", ExampleStatus.FAILING), outcome("GET /a", ExampleStatus.PASSED)])
    proposals = DocProposals(execution=head)
    mark_pre_existing(proposals, build_code_index([]), [], None, ExecutionReport(outcomes=list(head.outcomes)))

    gh = FakeCheckRepo()
    PreMergeChecker(gh).report(open_pr(), proposals, 0.5)
    body = gh.comments[1].body
    assert "## ✅ Docs are in sync with this PR" in body
    assert "<summary>Already stale on main, not caused by this PR (1)</summary>" in body
    assert "| `README.md` | example `POST /items` (line 5) | fails when run: status differs | ❌ no fix found |" in body
    assert "❌ still failing (pre-existing)" in body
    assert "Still failing — please review" not in body  # that list is for drift the PR introduced
    assert gh.statuses[-1][1:3] == ("success", "Docs are in sync with this PR (1 pre-existing issue on the base branch)")


def run_items_docs(files: dict[str, str]) -> ExecutionReport:
    docs = {p: t for p, t in files.items() if p.endswith(".md")}
    return execute_docs(files, docs, None, python=sys.executable)


def test_runs_the_base_app_to_tell_pre_existing_from_new_example_drift() -> None:
    """The fixture's README documents `200 OK` for `POST /items`, which returns 201."""
    head_files = fixture_files()
    head = run_items_docs(head_files)
    [failing] = head.failing
    assert failing.label == "POST /items"

    # Already 201 at the base: the docs were wrong before this PR.
    proposals = DocProposals(execution=head)
    mark_pre_existing(proposals, build_code_index([]), [], None, run_items_docs(head_files))
    assert failing.pre_existing and summarize(proposals, 0.5).state == "success"

    # 200 at the base: this PR changed the status code, so it broke the example.
    base_files = dict(head_files)
    base_files["app/main.py"] = head_files["app/main.py"].replace('@app.post("/items", status_code=201)', '@app.post("/items")')
    head = run_items_docs(head_files)
    proposals = DocProposals(execution=head)
    mark_pre_existing(proposals, build_code_index([]), [], None, run_items_docs(base_files))
    assert not head.failing[0].pre_existing and summarize(proposals, 0.5).state == "failure"


# --- static stale sections ---------------------------------------------------------------------

DOC = DocFile(path="README.md", content="# Notes\n\n## Listing\n\nCall `fetch_notes(limit=10)` to list notes. Set `HTTP_PROXY` if needed.\n")
CODE_20 = "def fetch_notes(limit=20):\n    return []\n"
CODE_10 = "def fetch_notes(limit=10):\n    return []\n"


def finding(doc: DocFile = DOC, terms: tuple[str, ...] = ("fetch_notes",)) -> StaleFinding:
    section = next(s for s in index_document(doc) if s.heading == "Listing")
    return StaleFinding(
        section=section,
        matched_terms=list(terms),
        verdict=SectionVerdict(stale=True, confidence=0.9, reason="Default is 20.", updated_section="## Listing\n\nx"),
        verification=Verification(status=VerificationStatus.VERIFIED),
    )


def classify(f: StaleFinding, head_code: str, base_code: str, base_doc: DocFile = DOC) -> bool:
    proposals = DocProposals(findings=[f])
    code = lambda text: build_code_index([SourceFile(path="notes.py", content=text)])  # noqa: E731
    mark_pre_existing(proposals, code(head_code), [base_doc], code(base_code), None)
    return proposals.findings[0].pre_existing


def test_section_wrong_against_the_base_code_too_is_pre_existing() -> None:
    assert classify(finding(), head_code=CODE_20, base_code=CODE_20)


def test_section_the_pr_made_wrong_is_the_prs() -> None:
    assert not classify(finding(), head_code=CODE_20, base_code=CODE_10)


def test_section_edited_in_the_pr_is_the_prs() -> None:
    older = DocFile(path="README.md", content=DOC.content.replace("to list notes", "to get notes"))
    assert not classify(finding(), head_code=CODE_20, base_code=CODE_20, base_doc=older)


def test_unrelated_standing_problems_do_not_excuse_the_pr() -> None:
    # `HTTP_PROXY` is unknown to the code at both commits, but the PR's term is `fetch_notes`, which is
    # correct at head: whatever the LLM found stale, nothing deterministic says it pre-dates the PR.
    assert not classify(finding(), head_code=CODE_10, base_code=CODE_10)


def test_baseline_is_skipped_when_there_is_nothing_to_classify() -> None:
    assert needs_baseline(DocProposals()) == (False, False)
    passing = ExecutionReport(outcomes=[outcome("GET /a", ExampleStatus.PASSED)])
    assert needs_baseline(DocProposals(execution=passing, findings=[finding()])) == (False, True)
