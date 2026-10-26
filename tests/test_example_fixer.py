from driftless.example_fixer import tidy_replacement

SNIPPET = """\
```bash
curl -X POST http://localhost:8000/items -d '{"name": "Pen"}'
```

`200 OK`
```json
{"id": 7, "name": "Pen", "created_at": "2024-01-01T00:00:00Z"}
```"""


def test_tidy_drops_wrapper_tags_and_restores_live_ids_and_timestamps() -> None:
    # What gpt-oss-120b actually returned in a live run.
    reply = (
        "<example>\n" + SNIPPET.replace("`200 OK`", "`201 Created`").replace(
            '{"id": 7, "name": "Pen", "created_at": "2024-01-01T00:00:00Z"}',
            '{"id":1,"name":"Pen","created_at":"2026-10-26T12:51:23.564456+00:00"}',
        ) + "\n</example>"
    )
    assert tidy_replacement(SNIPPET, reply) == SNIPPET.replace("`200 OK`", "`201 Created`")


def test_tidy_keeps_real_structure_changes_in_the_original_layout() -> None:
    reply = SNIPPET.replace('"name": "Pen", ', '"name": "Pen", "price": 1.5, ').replace('"id": 7', '"id": 3')
    tidied = tidy_replacement(SNIPPET, reply)
    assert '{"id": 7, "name": "Pen", "price": 1.5, "created_at": "2024-01-01T00:00:00Z"}' in tidied
