"""driftless-demo #7: MAX_BODY_LENGTH drops so low that the documented POST /notes gets a 422.

The bad fix documented the 422, then the 404s of the GET and DELETE that read the note. Happy
paths must stay happy: change the request so it succeeds, or flag it for a human and leave the
docs alone.
"""

import sys

from driftless.example_fixer import ExampleFixer
from driftless.example_impact import example_impact, impact_sentence
from driftless.executable_docs import DocRunner
from driftless.execution import ExampleFix, ExampleStatus
from driftless.pr_body import render_execution
from driftless.sandbox import Sandbox
from tests.test_sandbox import fixture_files

APP = """import os

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

MAX_BODY_LENGTH = int(os.getenv("MAX_BODY_LENGTH", "$limit"))

app = FastAPI(title="Notes")
_notes: dict[int, dict] = {}


class NoteIn(BaseModel):
    title: str
    body: str


@app.post("/notes", status_code=201)
def create_note(note: NoteIn):
    if len(note.body) > MAX_BODY_LENGTH:
        raise HTTPException(status_code=422, detail=f"body is longer than {MAX_BODY_LENGTH} characters")
    note_id = len(_notes) + 1
    _notes[note_id] = {"id": note_id, "title": note.title, "body": note.body}
    return _notes[note_id]


@app.get("/notes/{note_id}")
def get_note(note_id: int):
    if note_id not in _notes:
        raise HTTPException(status_code=404, detail="Note not found")
    return _notes[note_id]


@app.delete("/notes/{note_id}", status_code=204)
def delete_note(note_id: int):
    if _notes.pop(note_id, None) is None:
        raise HTTPException(status_code=404, detail="Note not found")
"""

DOCS = """# Notes

## Create a note

```bash
curl -X POST http://localhost:8000/notes \\
  -H "Content-Type: application/json" \\
  -d '{"title": "Groceries", "body": "Buy milk"}'
```

`201 Created`
```json
{"id": 1, "title": "Groceries", "body": "Buy milk"}
```

## Read a note

```bash
curl http://localhost:8000/notes/1
```

`200 OK`
```json
{"id": 1, "title": "Groceries", "body": "Buy milk"}
```

## Delete a note

```bash
curl -X DELETE http://localhost:8000/notes/1
```

`204 No Content`
"""

REJECTED_BODY = '`422 Unprocessable Entity`\n```json\n{"detail": "body is longer than 2 characters"}\n```'
CREATED = '`201 Created`\n```json\n{"id": 1, "title": "Groceries", "body": "Buy milk"}\n```'


class NotesLLM:
    """Plays the fixer through a list of replies to the POST, one per attempt.

    Each reply is "error" (documents the 422, as the live run did), "can't" (no valid request),
    or a new note body for the request. Being asked about any other example fails the test.
    """

    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)
        self.prompts: list[str] = []

    def complete(self, system, user, schema, check=None, output_tokens=0):
        assert schema is ExampleFix
        snippet = user.split("<example>\n", 1)[1].split("\n</example>", 1)[0]
        assert "curl -X POST" in snippet, "a 404 knock-on was sent to the fixer"
        self.prompts.append(user)
        kind = self.replies.pop(0)
        if kind == "can't":
            return ExampleFix(
                fixed=False, target="none", reason="MAX_BODY_LENGTH=2 rejects the example body.", replacement=None
            )
        if kind == "error":
            reply = ExampleFix(
                fixed=True, target="response", reason="POST /notes now returns 422.",
                replacement=snippet.replace(CREATED, REJECTED_BODY),
            )
        else:
            reply = ExampleFix(
                fixed=True, target="request", reason=f"The body must fit MAX_BODY_LENGTH; use {kind!r}.",
                replacement=snippet.replace('"body": "Buy milk"', f'"body": "{kind}"'),
            )
        if check:
            check(reply)
        return reply


def run_notes(limit: int, llm: NotesLLM):
    files = {**fixture_files(), "app/main.py": APP.replace("$limit", str(limit)), "README.md": DOCS}
    files.pop("docs/api.md")
    runner = DocRunner(Sandbox(files, python=sys.executable), ExampleFixer(llm))  # type: ignore[arg-type]
    return runner.execute({"README.md": DOCS}, files)


def test_limit_too_small_for_any_body_needs_a_human_and_no_404s_reach_the_docs() -> None:
    llm = NotesLLM("error", "can't")
    report = run_notes(2, llm)

    post, get, delete = report.outcomes
    assert (post.status, post.actual_status, post.final_expected_status) == (ExampleStatus.FAILING, 422, 201)
    assert post.needs_human == "MAX_BODY_LENGTH=2 rejects the example body."
    assert post.fixes == [] and post.root_cause
    for knock_on in (get, delete):
        assert (knock_on.status, knock_on.actual_status) == (ExampleStatus.FAILING, 404)
        assert knock_on.knock_on and knock_on.blocked_by == "POST /notes (line 5)" and not knock_on.root_cause
    assert report.contents["README.md"] == DOCS  # no 422, no 404s written into the docs

    # The model's 422 was rejected before running, and it was told why on its second attempt.
    assert len(llm.prompts) == 2
    assert "Never turn a documented 2xx into a 4xx" in llm.prompts[0]
    assert "it documented 422 instead of keeping the 201 success" in llm.prompts[1]

    assert impact_sentence(example_impact(report.outcomes), "This PR") == (
        "This PR breaks 1 documented example (`POST /notes`) in `README.md`: "
        "MAX_BODY_LENGTH=2 rejects the example body; needs a human. 2 later examples fail as a result."
    )
    body = "\n".join(render_execution(report))
    assert "| `README.md` | `POST /notes` (line 5) | 201 | 422 | ❌ needs a human |" in body
    assert "(needs a human: MAX_BODY_LENGTH=2 rejects the example body.)" in body


def test_reduced_limit_with_a_shorter_body_fixes_the_request_and_keeps_201() -> None:
    llm = NotesLLM("error", "Milk")
    report = run_notes(5, llm)

    post, get, delete = report.outcomes
    assert (post.status, post.actual_status) == (ExampleStatus.FIXED, 422)
    assert (post.expected_status, post.final_expected_status, post.final_actual_status) == (201, 201, 201)
    assert post.fixes == [
        "The body must fit MAX_BODY_LENGTH; use 'Milk'. Also updated it where documented responses echo it: "
        'GET /notes/1 (line 18): `body` "Buy milk" → "Milk".'
    ]
    assert [(o.status, o.caused_by) for o in (get, delete)] == [(ExampleStatus.DEPENDENT, "POST /notes (line 5)")] * 2
    readme = report.contents["README.md"]
    # The POST's request and response change, and so does the GET that echoes the new body.
    assert readme == DOCS.replace('"body": "Buy milk"', '"body": "Milk"')
    assert get.warnings == []
    assert "`201 Created`" in readme and "422" not in readme and "404" not in readme
    assert report.passing == report.total == 3


def test_a_fix_that_still_fails_when_run_is_thrown_away() -> None:
    llm = NotesLLM("Buy milk!", "can't")  # still too long for MAX_BODY_LENGTH=2
    report = run_notes(2, llm)

    post = report.outcomes[0]
    assert post.status == ExampleStatus.FAILING and post.fixes == []
    assert post.needs_human == "MAX_BODY_LENGTH=2 rejects the example body."
    assert "it still failed when run: status is 422, docs say 201" in llm.prompts[1]
    assert report.contents["README.md"] == DOCS


def test_rejected_fixes_alone_still_flag_the_example_for_a_human() -> None:
    report = run_notes(2, NotesLLM("error", "error"))
    post = report.outcomes[0]
    assert post.needs_human == "no fix keeps the documented 201 success (the request gets 422)"
    assert report.contents["README.md"] == DOCS
