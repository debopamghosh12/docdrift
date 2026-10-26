import sys

from driftless.analyzer import Analyzer
from driftless.doc_editor import final_docs
from driftless.example_fixer import ExampleFixer
from driftless.executable_docs import DocRunner, execute_all_docs
from driftless.execution import ExampleFix, ExampleStatus
from driftless.models import ChangedFile, DocFile, PullRequestInfo, SourceFile
from driftless.pipeline import analyze
from driftless.pr_body import render_execution
from driftless.pr_writer import PRWriter, PublishAction
from driftless.proposals import BatchSectionVerdict, BatchVerdicts, DocProposals
from driftless.sandbox import Sandbox
from tests.test_pr_writer import FakeGitHub
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
        ("README.md", "GET /greeting", ExampleStatus.PASSED),  # code default "unset" != docs "hello": a warning
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


class RuntimeOnlyLLM(StatusFixingLLM):
    """Static review finds nothing stale; the example fixer works as above."""

    def complete(self, system, user, schema, check=None, output_tokens=0):
        if schema is BatchVerdicts:
            ids = [part.split('"', 1)[0] for part in user.split('<section id="')[1:]]
            return BatchVerdicts(
                sections=[BatchSectionVerdict(id=i, stale=False, confidence=0.9, reason="still accurate") for i in ids]
            )
        return super().complete(system, user, schema, check, output_tokens)


def runtime_only_pr(llm: StatusFixingLLM):
    """A merged PR that only changes POST /items from 200 to 201: no symbol, route or env var changes."""
    files = fixture_files()
    after = files["app/main.py"]
    before = after.replace('@app.post("/items", status_code=201)', '@app.post("/items")')
    pr = PullRequestInfo(
        repo="me/items", number=7, title="Return 201 from POST /items", base_ref="main", merge_base_sha="a",
        head_sha="b", merge_commit_sha="c",
        files=[ChangedFile(path="app/main.py", before=before, after=after,
                           patch='@@ -24 +24 @@\n-@app.post("/items")\n+@app.post("/items", status_code=201)')],
    )
    docs = [DocFile(path=p, content=t) for p, t in sorted(files.items()) if p.endswith(".md")]
    result = analyze(pr, docs, sources=[SourceFile(path="app/main.py", content=after)])
    proposals = Analyzer(llm, workers=1).propose(result)  # type: ignore[arg-type]
    proposals.execution = execute_all_docs(
        result.docs, files, proposals, 0.5, ExampleFixer(llm), python=sys.executable  # type: ignore[arg-type]
    )
    return files, result, proposals


def test_runtime_only_pr_runs_all_docs_and_still_opens_a_docs_pr() -> None:
    files, result, proposals = runtime_only_pr(RuntimeOnlyLLM())
    assert not any(f.verdict.stale for f in proposals.findings) and proposals.insertions == []

    # Every doc file ran, although no rewrite touched any of them.
    report = proposals.execution
    assert {o.file for o in report.outcomes} == {"README.md", "docs/api.md"}
    assert [(o.label, o.status) for o in report.fixed] == [("POST /items", ExampleStatus.FIXED)]

    gh = FakeGitHub()
    gh.files = {d.path: d.content for d in result.docs}
    published = PRWriter(gh).publish(result, proposals, 0.5)

    assert published.action == PublishAction.OPENED and published.files == ["README.md"]
    committed = gh.commits[gh.branches["driftless/pr-7"]][2]
    assert committed == {"README.md": files["README.md"].replace(
        "-d '{\"name\": \"Pen\", \"price\": 1.5}'\n```\n\n`200 OK`",
        "-d '{\"name\": \"Pen\", \"price\": 1.5}'\n```\n\n`201 Created`",
    )}
    body = gh.pulls[100]["body"]
    assert "Updated 0 sections, added 0 sections, fixed 1 example after running across 1 file." in body
    assert "Static analysis found no stale sections." in body
    assert "### 🧪 Executed examples: 4/4 passing" in body
    assert "| File | Section | Change |" not in body and "Model reasoning" not in body


def test_runtime_only_pr_with_unfixable_examples_flags_them_on_the_source_pr() -> None:
    _, result, proposals = runtime_only_pr(RuntimeOnlyLLM(can_fix=False))
    gh = FakeGitHub()
    gh.files = {d.path: d.content for d in result.docs}
    published = PRWriter(gh).publish(result, proposals, 0.5)

    assert published.action == PublishAction.COMMENTED
    comment = next(iter(gh.comments.values())).body
    assert "❌ **Driftless:** 1 documented example failed against the app" in comment
    assert "`README.md` › `POST /items` (line 22): status is 201, docs say 200" in comment


CHAIN_DOCS = """# Items

## Create an item

```bash
curl -X POST http://localhost:8000/items \
  -H "Content-Type: application/json" \
  -d '{"name": "Pen"}'
```

`201 Created`
```json
{"id": 1, "name": "Pen", "price": 1.5, "created_at": "2024-01-01T00:00:00Z"}
```

## Get an item

```bash
curl http://localhost:8000/items/1
```

`200 OK`
```json
{"id": 1, "name": "Pen", "price": 1.5, "created_at": "2024-01-01T00:00:00Z"}
```
"""


class BodyFixingLLM:
    """Adds the missing `price` to the POST body; asked to fix anything else, it fails the test."""

    def __init__(self, can_fix: bool = True) -> None:
        self.labels: list[str] = []
        self.can_fix = can_fix

    def complete(self, system, user, schema, check=None, output_tokens=0):
        snippet = user.split("<example>\n", 1)[1].split("\n</example>", 1)[0]
        assert "curl -X POST" in snippet, "only the root cause may be sent to the fixer"
        self.labels.append("POST /items")
        if not self.can_fix:
            return ExampleFix(fixed=False, target="none", reason="unsure", replacement=None)
        reply = ExampleFix(
            fixed=True, target="request", reason="price is required.",
            replacement=snippet.replace('{"name": "Pen"}', '{"name": "Pen", "price": 1.5}'),
        )
        if check:
            check(reply)
        return reply


def run_chain(llm: BodyFixingLLM):
    files = {**fixture_files(), "docs/chain.md": CHAIN_DOCS}
    runner = DocRunner(Sandbox(files, python=sys.executable), ExampleFixer(llm))  # type: ignore[arg-type]
    return runner.execute({"docs/chain.md": CHAIN_DOCS}, files)


def test_later_example_that_passes_after_an_earlier_fix_is_dependent_not_stale() -> None:
    llm = BodyFixingLLM()
    report = run_chain(llm)

    post, get = report.outcomes
    assert (post.label, post.status, post.actual_status) == ("POST /items", ExampleStatus.FIXED, 422)
    assert (get.label, get.status, get.actual_status) == ("GET /items/1", ExampleStatus.DEPENDENT, 404)
    assert get.caused_by == "POST /items (line 5)" and get.fixes == []
    assert get.failures == ["status is 404, docs say 200"]
    assert llm.labels == ["POST /items"]  # the GET was never sent to the fixer
    assert [o.label for o in report.root_causes] == ["POST /items"] and report.passing == 2
    assert report.contents["docs/chain.md"].count('"price": 1.5}\'') == 1

    body = "\n".join(render_execution(report))
    assert "| ⛓️ failed only because of POST /items (line 5) |" in body
    assert "<summary>⛓️ 1 later example failed only because of an earlier one (it passes once that one is fixed)</summary>" in body
    assert "`docs/chain.md` › `GET /items/1` (line 16): failed only because of POST /items (line 5)" in body


def test_failures_after_an_unfixable_example_are_not_fixed_on_their_own() -> None:
    llm = BodyFixingLLM(can_fix=False)
    report = run_chain(llm)

    post, get = report.outcomes
    assert post.status == ExampleStatus.FAILING and post.blocked_by is None and post.root_cause
    assert get.status == ExampleStatus.FAILING and get.blocked_by == "POST /items (line 5)" and not get.root_cause
    assert get.knock_on and post.needs_human == "unsure" and post.note == "needs a human: unsure"
    assert get.note == "not auto-fixed: POST /items (line 5) failed in this run, so what it reads was never created"
    assert llm.labels == ["POST /items"]  # writing the GET's 404 into the docs would be wrong
    assert report.contents["docs/chain.md"] == CHAIN_DOCS


ECHO_DOCS = r"""# Items

## Create an item

```bash
curl -X POST http://localhost:8000/items \
  -H "Content-Type: application/json" \
  -d '{"name": "Pen"}'
```

`201 Created`
```json
{"id": 1, "name": "Pen", "price": 1.5, "created_at": "2024-01-01T00:00:00Z"}
```

## Get an item

```bash
curl http://localhost:8000/items/1
```

`200 OK`
```json
{"id": 1, "name": "Pen", "price": 1.5, "created_at": "2024-01-01T00:00:00Z"}
```

## Create another item

```bash
curl -X POST http://localhost:8000/items \
  -H "Content-Type: application/json" \
  -d '{"name": "Pen", "price": 2.0}'
```

`201 Created`
```json
{"id": 2, "name": "Pen", "price": 2.0, "created_at": "2024-01-01T00:00:00Z"}
```
"""


class RenamingLLM:
    """Fixes the POST by adding `price` and renaming the item, in its request and its own response."""

    def complete(self, system, user, schema, check=None, output_tokens=0):
        snippet = user.split("<example>\n", 1)[1].split("\n</example>", 1)[0]
        replacement = snippet.replace('{"name": "Pen"}', '{"name": "Ink", "price": 1.5}').replace(
            '"name": "Pen", "price": 1.5', '"name": "Ink", "price": 1.5'
        )
        reply = ExampleFix(fixed=True, target="request", reason="price is required", replacement=replacement)
        if check:
            check(reply)
        return reply


def test_a_fixed_request_value_is_updated_where_later_responses_echo_it() -> None:
    files = {**fixture_files(), "docs/echo.md": ECHO_DOCS}
    runner = DocRunner(Sandbox(files, python=sys.executable), ExampleFixer(RenamingLLM()))  # type: ignore[arg-type]
    report = runner.execute({"docs/echo.md": ECHO_DOCS}, files)

    post, get, other = report.outcomes
    assert (post.status, get.status, other.status) == (ExampleStatus.FIXED, ExampleStatus.DEPENDENT, ExampleStatus.PASSED)
    assert report.passing == report.total == 3
    assert not any("name" in w for o in report.outcomes for w in o.warnings)  # no value warnings left for it
    assert post.fixes[0].startswith("price is required. Also updated")  # a reason without a full stop gets one
    assert 'Also updated it where documented responses echo it: GET /items/1 (line 18): `name` "Pen" → "Ink"' in post.fixes[0]

    text = report.contents["docs/echo.md"]
    get_section = text.split("## Get an item", 1)[1].split("## Create another item", 1)[0]
    assert '"name": "Ink", "price": 1.5' in get_section and "Pen" not in get_section
    # The later POST sends "Pen" on purpose, and its response echoes that: neither is touched.
    other_section = text.split("## Create another item", 1)[1]
    assert other_section == ECHO_DOCS.split("## Create another item", 1)[1]
