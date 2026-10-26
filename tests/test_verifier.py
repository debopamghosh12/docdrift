from docdrift.code_index import build_code_index
from docdrift.minimal_diff import restore_formatting, unrelated_change_flag
from docdrift.models import SourceFile
from docdrift.verifier import (
    Verifier,
    check_code_blocks,
    check_example_values,
    check_routes,
    check_signatures,
    check_symbols,
    route_matches,
)

CONFIG = '''import os

APP_PORT = int(os.getenv("APP_PORT", "8000"))
MAX_BODY_LENGTH = int(os.getenv("MAX_BODY_LENGTH", "1000"))
NOTES_PAGE_SIZE = int(os.getenv("NOTES_PAGE_SIZE", "20"))
'''
SERVICE = '''from app.config import MAX_BODY_LENGTH


def fetch_notes(limit: int = 20, offset: int = 0) -> list[dict]:
    return []


def update_note(note_id: int, title: str | None = None, body: str | None = None) -> dict | None:
    return None
'''
MAIN = '''from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

from app import service
from app.config import NOTES_PAGE_SIZE

app = FastAPI()


class NotePatch(BaseModel):
    title: str | None = None


@app.get("/notes")
def list_notes(limit: int = Query(NOTES_PAGE_SIZE, ge=1), offset: int = Query(0, ge=0)):
    return service.fetch_notes(limit=limit, offset=offset)


@app.patch("/notes/{note_id}")
def update_note(note_id: int, patch: NotePatch):
    return None


@app.delete("/notes/{note_id}", status_code=204)
def delete_note(note_id: int):
    return None
'''
CODE = build_code_index(
    [
        SourceFile(path="app/config.py", content=CONFIG),
        SourceFile(path="app/service.py", content=SERVICE),
        SourceFile(path="app/main.py", content=MAIN),
    ]
)


# --- 1. signatures -----------------------------------------------------------


def test_notes_page_size_default_bug_from_last_run() -> None:
    doc = "### `fetch_notes(limit=NOTES_PAGE_SIZE, offset=0)`\n- `limit` (`int`, default `NOTES_PAGE_SIZE`)\n"
    assert check_signatures(doc, CODE) == [
        "fetch_notes: default of limit is 20 in code, doc says NOTES_PAGE_SIZE (app/service.py)"
    ]


def test_correct_signature_passes() -> None:
    assert check_signatures("### `fetch_notes(limit=20, offset=0)`", CODE) == []


def test_fastapi_query_default_and_constant_resolution() -> None:
    # Code default is Query(NOTES_PAGE_SIZE) and NOTES_PAGE_SIZE = int(os.getenv(..., "20")).
    assert check_signatures("`list_notes(limit=NOTES_PAGE_SIZE, offset=0)`", CODE) == []
    assert check_signatures("`list_notes(limit=20, offset=0)`", CODE) == []
    assert check_signatures("`list_notes(limit=50)`", CODE) == [
        "list_notes: default of limit is NOTES_PAGE_SIZE in code, doc says 50 (app/main.py)"
    ]


def test_same_name_in_two_files_matches_either_definition() -> None:
    # service.update_note(note_id, title, body) vs main.update_note(note_id, patch)
    assert check_signatures("### `update_note(note_id, title=None, body=None)`", CODE) == []


def test_unknown_param_order_and_missing_required() -> None:
    failures = check_signatures("`fetch_notes(offset=0, limit=20, page=1)`", CODE)
    assert "fetch_notes: parameter page does not exist in code" in failures[0]
    assert failures[1] == "fetch_notes: parameter order is limit, offset in code, doc says offset, limit (app/service.py)"
    assert check_signatures("`delete_note()`", CODE) == [
        "delete_note: required parameter note_id is missing from the doc (app/main.py)"
    ]


def test_usage_examples_and_python_defs() -> None:
    assert check_signatures("`fetch_notes(5)`", CODE) == []  # a call example, not a signature
    block = "```python\ndef fetch_notes(limit=10, offset=0):\n    ...\n```"
    assert check_signatures(block, CODE) == [
        "fetch_notes: default of limit is 20 in code, doc says 10 (app/service.py)"
    ]


# --- 2. symbols --------------------------------------------------------------


def test_new_unknown_symbols_fail() -> None:
    updated = "Call `paginate_notes(limit)` with `NoteFilter`; set `NOTES_MAX_PAGES`."
    assert check_symbols("", updated, CODE) == [
        "`paginate_notes()` is not defined anywhere in the repo at the merge commit",
        "class `NoteFilter` does not exist in the repo at the merge commit",
        "`NOTES_MAX_PAGES` is not an env var or constant in the repo at the merge commit",
    ]


def test_known_builtin_imported_and_original_symbols_pass() -> None:
    original = "Uses `legacy_helper()` and `OLD_SETTING`."
    updated = (
        "Uses `legacy_helper()` and `OLD_SETTING`. Raises `ValueError` or `HTTPException`; "
        "body is a `NotePatch`; limit from `NOTES_PAGE_SIZE`; see `fetch_notes(limit=20)`.\n"
        '```json\n{"detail": "body exceeds MAX_BODY_LENGTH (1000)"}\n```'
    )
    assert check_symbols(original, updated, CODE) == []


def test_cli_flags_checked_only_for_projects_with_a_cli() -> None:
    assert check_symbols("", "Run `tool --verbose`.", CODE) == []  # demo app has no CLI flags
    cli = build_code_index([SourceFile(path="cli.py", content='p.add_argument("--quiet")\n')])
    assert check_symbols("", "Run `tool --verbose`.", cli) == ["CLI flag `--verbose` does not exist in the code"]
    assert check_symbols("", "```bash\ncurl --header x http://h/\n```", cli) == []  # external tool


# --- 3. routes ---------------------------------------------------------------


def test_routes_must_exist_with_method() -> None:
    updated = "`PATCH /notes/{note_id}` and `PUT /notes/{id}` and `GET /users`"
    assert check_routes("", updated, CODE) == [
        "route GET /users does not exist in the code; no route has that path",
        "route PUT /notes/{id} does not exist in the code; that path supports DELETE, PATCH",
    ]


def test_curl_examples_and_concrete_paths() -> None:
    ok = "```bash\ncurl -X PATCH http://localhost:8000/notes/1 -d '{}'\n```"
    bad = "```bash\ncurl -X POST http://localhost:8000/notes/1\n```"
    assert check_routes("", ok, CODE) == []
    assert check_routes("", bad, CODE) == [
        "route POST /notes/1 does not exist in the code; that path supports DELETE, PATCH"
    ]


def test_routes_already_in_original_are_exempt() -> None:
    assert check_routes("`GET /legacy`", "`GET /legacy` (unchanged)", CODE) == []
    assert route_matches("/notes/:id", "/notes/{note_id}")
    assert not route_matches("/notes", "/notes/{note_id}")


# --- example values ----------------------------------------------------------


def test_invented_example_values_fail_from_last_run() -> None:
    # Seen in a real run: the model added NOTES_PAGE_SIZE=30, a value from nowhere.
    original = "```bash\nAPP_PORT=9000 MAX_NOTE_LENGTH=500 python -m app.main\n```"
    updated = "```bash\nAPP_PORT=9000 MAX_BODY_LENGTH=500 NOTES_PAGE_SIZE=30 python -m app.main\n```"
    assert check_example_values(original, updated, CODE) == [
        "example value NOTES_PAGE_SIZE=30 is not in the code or the original docs (default in code: 20); "
        "use the code's value or leave the setting out of the example"
    ]
    # The renamed setting keeps the original's value (500); the code default is always fine.
    assert check_example_values(original, updated.replace("=30", "=20"), CODE) == []
    assert CODE.env_defaults["NOTES_PAGE_SIZE"] == "20"


def test_example_values_ignore_unknown_names() -> None:
    assert check_example_values("", "`PYTHONPATH=src pytest`", CODE) == []


# --- 4. code blocks ----------------------------------------------------------


def test_python_blocks_must_parse() -> None:
    bad = "```python\nnotes = service.fetch_notes(limit=\n```"
    (failure,) = check_code_blocks("", bad)
    assert failure.startswith("python code block does not parse (line 1")
    assert check_code_blocks("", "```python\nprint(1)\n```\n```bash\nnot python (\n```") == []
    assert check_code_blocks(bad, bad) == []  # unchanged from the original


# --- 5. minimal diff ---------------------------------------------------------

TABLE = """\
| Variable          | Default     | Description |
|-------------------|-------------|-------------|
| `APP_PORT`        | `8000`      | Port the server listens on. |
| `MAX_NOTE_LENGTH` | `1000`      | Maximum body length. |
"""


def test_table_repadding_is_restored_and_new_rows_are_padded() -> None:
    # What the model produced last run: re-padded the whole table while renaming one row.
    rewrite = """\
| Variable          | Default | Description |
|-------------------|---------|-------------|
| `APP_PORT`        | `8000`  | Port the server listens on. |
| `MAX_BODY_LENGTH` | `1000`  | Maximum body length. |
| `NOTES_PAGE_SIZE` | `20`    | Default page size. |
"""
    text, restored = restore_formatting(TABLE, rewrite)
    assert restored == 3
    assert text == """\
| Variable          | Default     | Description |
|-------------------|-------------|-------------|
| `APP_PORT`        | `8000`      | Port the server listens on. |
| `MAX_BODY_LENGTH` | `1000`      | Maximum body length. |
| `NOTES_PAGE_SIZE` | `20`        | Default page size. |
"""


def test_whitespace_only_changes_are_restored() -> None:
    original = "Some  text\n- item one\n\n```bash\n  run   it\n```\n"
    text, restored = restore_formatting(original, "Some text\n- item  one\n\n```bash\nrun it\n```\nNew line.\n")
    assert text == "Some  text\n- item one\n\n```bash\n  run   it\n```\nNew line.\n"
    assert restored == 3


def test_typographic_lookalikes_are_restored() -> None:
    # Seen in a real run: "in-memory" came back with a non-breaking hyphen (U+2011).
    original = "| `DATABASE_URL` | `memory://` | Storage is always in-memory — for now. |\n"
    rewrite = "| `DATABASE_URL` | `memory://` | Storage is always in‑memory — for now. |\n"
    assert restore_formatting(original, rewrite) == (original, 1)
    # A real wording change is kept.
    changed = original.replace("always", "currently")
    assert restore_formatting(original, changed) == (changed, 0)


def test_unrelated_rewrite_is_flagged() -> None:
    original = "Intro line.\nSecond line.\nThird line.\nFourth line.\nUses `OLD_NAME`.\n"
    small = original.replace("OLD_NAME", "NEW_NAME")
    large = "Rewritten intro.\nAnother.\nMore.\nFourth line.\nUses `NEW_NAME`.\n"
    assert unrelated_change_flag(original, small, ["OLD_NAME", "NEW_NAME"]) is None
    flag = unrelated_change_flag(original, large, ["OLD_NAME", "NEW_NAME"])
    assert flag is not None and flag.startswith("rewrote 3 of 5 original lines")


def test_verifier_combines_checks() -> None:
    verifier = Verifier(CODE, ["MAX_NOTE_LENGTH", "MAX_BODY_LENGTH"])
    report = verifier.check(TABLE, TABLE.replace("MAX_NOTE_LENGTH", "MAX_BODY_LENGTH").replace("     |", " |"))
    assert report.ok and report.restored_lines > 0
    bad = verifier.check("## Notes\n", "## Notes\n`fetch_notes(limit=NOTES_PAGE_SIZE)`\n")
    assert not bad.ok
