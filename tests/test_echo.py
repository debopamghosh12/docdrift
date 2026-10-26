"""echo.py: which documented response values follow a fixed request, and which never do."""

from driftless.echo import changed_request_values, echo_request_values
from driftless.examples import extract_examples
from driftless.executor import ExampleRun

DOC = """```bash
curl -X POST http://localhost:8000/notes -d '{"title": "Groceries", "body": "Milk"}'
```

`201 Created`
```json
{"id": 1, "title": "Groceries", "body": "Milk"}
```

```bash
curl http://localhost:8000/notes
```

`200 OK`
```json
{"items": [{"id": 1, "title": "Groceries", "body": "Milk"}, {"id": 2, "title": "Ideas", "body": "Milk"}]}
```

```bash
curl http://localhost:8000/notes/1
```

`200 OK`
```json
{"id": 1, "title": "Groceries", "body": "Milk", "note": "Milk"}
```
"""


def runs_for(text: str, bodies: list[str]) -> list[ExampleRun]:
    return [ExampleRun(example=e, actual_status=200, actual_body=b) for e, b in zip(extract_examples("a.md", text), bodies)]


def test_changed_request_values_are_scalar_leaves_by_key() -> None:
    before, = extract_examples("a.md", DOC)[:1]
    after = before.model_copy(update={"body": '{"title": "Groceries", "body": "Tea"}'})
    assert changed_request_values(before, after) == [("body", "Milk", "Tea")]
    assert changed_request_values(before, before) == []
    assert changed_request_values(before.model_copy(update={"body": "not json"}), after) == []


def test_only_fields_the_live_response_echoes_are_updated() -> None:
    fixed = DOC.replace('"body": "Milk"}\'', '"body": "Tea"}\'', 1)
    runs = runs_for(fixed, [
        '{"id": 1, "title": "Groceries", "body": "Tea"}',
        '{"items": [{"id": 1, "title": "Groceries", "body": "Tea"}]}',  # one item: the list doesn't line up
        '{"id": 1, "title": "Groceries", "body": "Tea", "note": "Milk"}',
    ])
    text, notes = echo_request_values(fixed, 0, [("body", "Milk", "Tea")], runs)

    first, listing, single = text.split("```bash")[1:]
    assert '{"id": 1, "title": "Groceries", "body": "Tea"}' in first  # the fixed example's own response
    assert listing == fixed.split("```bash")[2]  # 2 documented "Milk" bodies, only 1 echoed: untouched
    assert '"body": "Tea", "note": "Milk"' in single  # other keys holding the same value are not echoes
    assert len(notes) == 2 and notes[1] == 'GET /notes/1 (line 19): `body` "Milk" → "Tea"'


def test_requests_and_earlier_examples_are_never_edited() -> None:
    doc = DOC.replace("curl http://localhost:8000/notes/1", "curl -X PUT http://localhost:8000/notes/1 -d '{\"body\": \"Milk\"}'")
    runs = runs_for(doc, ['{"body": "Tea"}', "{}", '{"id": 1, "title": "Groceries", "body": "Tea", "note": "Milk"}'])
    text, notes = echo_request_values(doc, 1, [("body", "Milk", "Tea")], runs)
    assert "-d '{\"body\": \"Milk\"}'" in text  # a later request keeps its own value
    assert text.split("```bash")[1] == doc.split("```bash")[1]  # before the fixed example
    assert notes == ['PUT /notes/1 (line 19): `body` "Milk" → "Tea"']
