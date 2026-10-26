from driftless.examples import documented_env_defaults, extract_examples, parse_curl, parse_status

DOC = """\
# Notes

## Create

```bash
curl -X POST http://localhost:8000/notes \\
  -H "Content-Type: application/json" \\
  -d '{"title": "Groceries", "body": "Milk"}'
```

`201 Created`
```json
{"id": 1, "title": "Groceries", "body": "Milk"}
```

`422 Unprocessable Entity` if `body` is too long:
```json
{"detail": "too long"}
```

## List

```http
GET /notes?limit=2
```

**Response — `200 OK`**
```json
[{"id": 1, "title": "Groceries", "body": "Milk"}]
```

## Delete

```sh
$ curl -X DELETE "http://127.0.0.1:8000/notes/1"
```

`204 No Content` on success.

## Elsewhere

```bash
curl https://api.example.com/notes
curl "http://localhost:8000/notes/$NOTE_ID"
pip install -r requirements.txt
```
"""


def test_extracts_requests_with_their_documented_results_in_order() -> None:
    examples = extract_examples("README.md", DOC)
    assert [(e.label, e.expected_status) for e in examples] == [
        ("POST /notes", 201),
        ("GET /notes", 200),
        ("DELETE /notes/1", 204),
    ]
    create, listing, delete = examples
    assert create.headers == {"Content-Type": "application/json"}
    assert create.body == '{"title": "Groceries", "body": "Milk"}'
    # The first status and the JSON after it; the 422 case documents a different request.
    assert create.expected_json == {"id": 1, "title": "Groceries", "body": "Milk"}
    lines = DOC.split("\n")
    assert lines[create.start_line] == "```bash"
    assert lines[create.end_line - 1] == "```" and "Groceries" in lines[create.end_line - 2]
    assert listing.path == "/notes?limit=2" and listing.has_expected_json
    assert delete.has_expected_json is False


def test_parse_curl_variants() -> None:
    assert parse_curl("curl -s http://localhost:8000/x | jq .") is not None
    json_post = parse_curl("curl --json '{\"a\": 1}' localhost:8000/x")
    assert json_post is not None and json_post.method == "POST"
    assert json_post.headers == {"Content-Type": "application/json"}
    assert parse_curl("curl -XPUT http://localhost:8000/x").method == "PUT"  # type: ignore[union-attr]
    assert parse_curl("curl -d @body.json http://localhost:8000/x") is None
    assert parse_curl("wget http://localhost:8000/x") is None


def test_parse_status_needs_a_reason_phrase() -> None:
    assert parse_status("`201 Created`") == 201
    assert parse_status("HTTP/1.1 404 Not Found") == 404
    assert parse_status("must be at most 1000 characters") is None
    assert parse_status("serves on port 8000 by default") is None


def test_documented_env_defaults_come_from_default_columns() -> None:
    doc = """\
| Variable | Default | Description |
|----------|---------|-------------|
| `APP_PORT` | `8000` | Port. |
| `DATABASE_URL` | `memory://` | DB. |
| `NO_DEFAULT` | none | x |

| Name | Type |
|------|------|
| `limit` | `int` |
"""
    assert documented_env_defaults(doc) == {"APP_PORT": "8000", "DATABASE_URL": "memory://"}
