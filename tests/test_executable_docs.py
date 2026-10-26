import sys

from driftless.doc_editor import final_docs
from driftless.example_fixer import ExampleFixer
from driftless.executable_docs import DocRunner
from driftless.execution import ExampleFix, ExampleStatus
from driftless.models import DocFile
from driftless.pr_body import render_execution
from driftless.proposals import DocProposals
from driftless.sandbox import Sandbox
from tests.test_sandbox import fixture_files


class StatusFixingLLM:
    """Fixes a documented `200 OK` to `201 Created`, like the model would from the actual response."""

    def __init__(self, can_fix: bool = True) -> None:
        self.prompts: list[str] = []
        self.can_fix = can_fix

    def complete(self, system, user, schema, check=None, output_tokens=0):
        assert schema is ExampleFix
        self.prompts.append(user)
        snippet = user.split("<example>\n", 1)[1].split("\n</example>", 1)[0]
        if self.can_fix:
            reply = ExampleFix(
                fixed=True, target="response", reason="POST /items returns 201, not 200.",
                replacement=snippet.replace("`200 OK`", "`201 Created`"),
            )
        else:
            reply = ExampleFix(fixed=False, target="none", reason="unsure", replacement=None)
        if check:
            check(reply)
        return reply


def run(llm: StatusFixingLLM | None):
    files = fixture_files()
    docs = {p: t for p, t in files.items() if p.endswith(".md")}
    fixer = ExampleFixer(llm) if llm else None  # type: ignore[arg-type]
    runner = DocRunner(Sandbox(files, python=sys.executable), fixer)
    return files, runner.execute(docs, files)


def outcomes(report):
    return [(o.file, o.label, o.status) for o in report.outcomes]


def test_wrong_documented_status_is_fixed_after_running() -> None:
    llm = StatusFixingLLM()
    files, report = run(llm)

    assert outcomes(report) == [
        ("README.md", "GET /greeting", ExampleStatus.PASSED),  # GREETING default from the docs table
        ("README.md", "POST /items", ExampleStatus.FIXED),
        ("README.md", "GET /items/1", ExampleStatus.PASSED),  # reads what the POST created
        ("docs/api.md", "GET /items/1", ExampleStatus.PASSED),  # fresh app per file: 404
    ]
    fixed = report.outcomes[1]
    assert (fixed.expected_status, fixed.actual_status, fixed.final_expected_status) == (200, 201, 201)
    assert fixed.failures == ["status is 201, docs say 200"]
    assert fixed.fixes == ["POST /items returns 201, not 200."]
    assert report.passing == report.total == 4
    assert len(llm.prompts) == 1
    assert "Response status: 201" in llm.prompts[0] and '@app.post("/items"' in llm.prompts[0]

    readme = report.contents["README.md"]
    assert readme.count("`201 Created`") == 1 and readme.count("`200 OK`") == 2
    assert readme == files["README.md"].replace("-d '{\"name\": \"Pen\", \"price\": 1.5}'\n```\n\n`200 OK`",
                                                "-d '{\"name\": \"Pen\", \"price\": 1.5}'\n```\n\n`201 Created`")


def test_unfixable_example_is_kept_and_flagged() -> None:
    _, report = run(StatusFixingLLM(can_fix=False))
    failing = report.failing
    assert [(o.label, o.status) for o in failing] == [("POST /items", ExampleStatus.FAILING)]
    assert failing[0].note == "model could not fix it: unsure"
    assert report.passing == 3

    body = "\n".join(render_execution(report))
    assert "### 🧪 Executed examples: 3/4 passing" in body
    assert "| `README.md` | `POST /items` (line 22) | 200 | 201 | ❌ still failing |" in body
    assert "Still failing" in body


def test_without_a_fixer_examples_are_only_reported() -> None:
    _, report = run(None)
    assert [o.status for o in report.outcomes].count(ExampleStatus.FAILING) == 1
    assert report.fixed == []


def test_example_fixes_are_part_of_the_published_docs() -> None:
    files, report = run(StatusFixingLLM())
    docs = [DocFile(path=p, content=t) for p, t in files.items() if p.endswith(".md")]
    proposals = DocProposals(execution=report)
    assert final_docs(docs, proposals, 0.5) == {"README.md": report.contents["README.md"]}
    body = "\n".join(render_execution(report))
    assert "| `README.md` | `POST /items` (line 22) | ~~200~~ 201 | 201 | 🔁 fixed after running |" in body
    assert "POST /items returns 201, not 200. (was: status is 201, docs say 200)" in body
