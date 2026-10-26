from pathlib import Path

from docdrift.diff_parser import build_changeset, parse_unified_diff
from docdrift.diff_parser.patch import reconstruct
from docdrift.models import ChangedFile, ChangeKind, ChangeSet, FileStatus, SymbolKind

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> list[ChangedFile]:
    return parse_unified_diff((FIXTURES / name).read_text())


def symbol_changes(cs: ChangeSet) -> dict[str, ChangeKind]:
    return {s.name: s.change for s in cs.symbols}


def names(items: list, attr: str, change: ChangeKind) -> set[str]:
    return {getattr(i, attr) for i in items if i.change == change}


# --- unified diff parsing ---------------------------------------------------


def test_parse_unified_diff_statuses_and_paths() -> None:
    files = {f.path: f for f in load("multi_file.diff")}
    assert files["src/new_module.py"].status == FileStatus.ADDED
    assert files["src/old_module.py"].status == FileStatus.REMOVED
    renamed = files["src/after.py"]
    assert renamed.status == FileStatus.RENAMED
    assert renamed.previous_path == "src/before.py"
    assert renamed.patch.startswith("@@")


def test_reconstruct_splits_before_and_after() -> None:
    patch = "@@ -1,2 +1,2 @@\n keep\n-old\n+new"
    before, after = reconstruct(patch)
    assert before.split() == ["keep", "old"]
    assert after.split() == ["keep", "new"]


def test_added_and_removed_files_from_patch_only() -> None:
    cs = build_changeset(load("multi_file.diff"))
    changes = symbol_changes(cs)
    assert changes["created"] == ChangeKind.ADDED
    assert changes["doomed"] == ChangeKind.REMOVED


# --- Python (ast) -----------------------------------------------------------

BEFORE_PY = '''
import os

API_URL = os.getenv("API_URL")

def fetch_user(user_id: int) -> dict:
    return {"id": user_id}

def old_helper(x):
    total = x * 2
    return total

class Client:
    def get(self, path):
        return path

    def close(self):
        pass
'''

AFTER_PY = '''
import os

API_URL = os.getenv("API_BASE_URL")
TIMEOUT = os.environ.get("API_TIMEOUT", "5")

def fetch_user(user_id: int, include_posts: bool = False) -> dict:
    return {"id": user_id}

def new_helper(x):
    total = x * 2
    return total

def brand_new():
    return 1

class Client:
    def get(self, path):
        return path.strip()
'''


def python_changeset(before: str, after: str) -> ChangeSet:
    return build_changeset([ChangedFile(path="app/core.py", before=before, after=after)])


def test_python_ast_symbol_changes() -> None:
    cs = python_changeset(BEFORE_PY, AFTER_PY)
    changes = symbol_changes(cs)
    assert changes == {
        "fetch_user": ChangeKind.SIGNATURE_CHANGED,
        "new_helper": ChangeKind.RENAMED,
        "brand_new": ChangeKind.ADDED,
        "Client.get": ChangeKind.MODIFIED,
        "Client.close": ChangeKind.REMOVED,
    }


def test_python_signature_and_rename_details() -> None:
    cs = python_changeset(BEFORE_PY, AFTER_PY)
    by_name = {s.name: s for s in cs.symbols}
    fetch = by_name["fetch_user"]
    assert fetch.old_signature == "fetch_user(user_id: int) -> dict"
    assert fetch.new_signature == "fetch_user(user_id: int, include_posts: bool=False) -> dict"
    assert by_name["new_helper"].old_name == "old_helper"
    assert by_name["Client.get"].kind == SymbolKind.METHOD


def test_python_env_var_changes() -> None:
    cs = python_changeset(BEFORE_PY, AFTER_PY)
    assert names(cs.env_vars, "name", ChangeKind.REMOVED) == {"API_URL"}
    assert names(cs.env_vars, "name", ChangeKind.ADDED) == {"API_BASE_URL", "API_TIMEOUT"}


def test_pydantic_settings_fields_become_env_vars() -> None:
    before = "class Settings(BaseSettings):\n    github_token: str = ''\n"
    after = (
        "class Settings(BaseSettings):\n"
        "    model_config = SettingsConfigDict(env_prefix='DD_')\n"
        "    github_token: str = ''\n"
        "    llm_model: str = 'x'\n"
    )
    cs = python_changeset(before, after)
    assert names(cs.env_vars, "name", ChangeKind.REMOVED) == {"GITHUB_TOKEN"}
    assert names(cs.env_vars, "name", ChangeKind.ADDED) == {"DD_GITHUB_TOKEN", "DD_LLM_MODEL"}


def test_class_field_change_marks_class_modified() -> None:
    before = "class Config:\n    retries = 3\n    def run(self):\n        pass\n"
    after = "class Config:\n    retries = 5\n    def run(self):\n        pass\n"
    assert symbol_changes(python_changeset(before, after)) == {"Config": ChangeKind.MODIFIED}


def test_private_symbols_are_flagged() -> None:
    cs = python_changeset("", "def _internal():\n    pass\n\nclass A:\n    def __init__(self):\n        pass\n")
    public = {s.name: s.public for s in cs.symbols}
    assert public == {"_internal": False, "A": True, "A.__init__": True}


def test_python_patch_fixture() -> None:
    cs = build_changeset(load("python_patch.diff"))
    assert symbol_changes(cs) == {
        "greet": ChangeKind.SIGNATURE_CHANGED,
        "farewell": ChangeKind.REMOVED,
        "welcome": ChangeKind.ADDED,
    }
    welcome = next(s for s in cs.symbols if s.name == "welcome")
    assert welcome.new_signature == "async welcome(user: str) -> str"
    assert names(cs.env_vars, "name", ChangeKind.REMOVED) == {"GREETING_LANG"}
    assert names(cs.env_vars, "name", ChangeKind.ADDED) == {"APP_LANG"}


def test_unparseable_python_falls_back_to_regex() -> None:
    before = "    def handle(self, event):\n        if True:\n"
    after = "    def handle(self, event, retry=False):\n        if True:\n"
    cs = python_changeset(before, after)
    assert len(cs.symbols) == 1
    symbol = cs.symbols[0]
    assert symbol.change == ChangeKind.SIGNATURE_CHANGED
    assert symbol.kind == SymbolKind.METHOD
    assert symbol.new_signature == "handle(self, event, retry=False)"


# --- FastAPI routes ---------------------------------------------------------


def test_fastapi_route_changes() -> None:
    before = '@app.get("/items/{item_id}")\ndef read(item_id: int):\n    return item_id\n'
    after = (
        '@router.get(path="/v2/items/{item_id}")\ndef read(item_id: int):\n    return item_id\n\n'
        '@app.post("/items")\ndef create():\n    return {}\n'
    )
    cs = python_changeset(before, after)
    routes = {r.label: r for r in cs.routes}
    assert {label: r.change for label, r in routes.items()} == {
        "GET /v2/items/{item_id}": ChangeKind.MODIFIED,  # same handler, new decorator path
        "POST /items": ChangeKind.ADDED,
    }
    assert routes["GET /v2/items/{item_id}"].old_label == "GET /items/{item_id}"
    assert routes["POST /items"].handler == "create"


def test_fastapi_decorator_method_change_is_modified_route() -> None:
    before = '@app.put("/notes/{note_id}")\ndef update_note(note_id: int):\n    return note_id\n'
    after = '@app.patch("/notes/{note_id}")\ndef update_note(note_id: int):\n    return note_id\n'
    (route,) = python_changeset(before, after).routes
    assert (route.label, route.change, route.old_label) == (
        "PATCH /notes/{note_id}",
        ChangeKind.MODIFIED,
        "PUT /notes/{note_id}",
    )


def test_handler_signature_change_marks_route_modified_and_tags_symbol() -> None:
    before = '@app.get("/notes")\ndef list_notes(limit: int = 20):\n    return []\n'
    after = '@app.get("/notes")\ndef list_notes(limit: int = 20, offset: int = 0):\n    return []\n'
    cs = python_changeset(before, after)
    assert [(r.label, r.change, r.handler) for r in cs.routes] == [
        ("GET /notes", ChangeKind.MODIFIED, "list_notes")
    ]
    assert cs.symbols[0].route == "GET /notes"


def test_api_route_with_methods() -> None:
    after = '@router.api_route("/sync", methods=["GET", "POST"])\nasync def sync():\n    pass\n'
    cs = python_changeset("", after)
    assert {r.label for r in cs.routes} == {"GET /sync", "POST /sync"}


def test_fuzzy_rename_requires_similar_body_and_shared_name_token() -> None:
    before = (
        "def list_notes(limit: int = 20) -> list[dict]:\n"
        '    """Return up to `limit` notes, oldest first."""\n'
        "    return list(_notes.values())[:limit]\n"
    )
    after = (
        "def fetch_notes(limit: int = 20, offset: int = 0) -> list[dict]:\n"
        '    """Return up to `limit` notes, oldest first, skipping `offset`."""\n'
        "    return list(_notes.values())[offset : offset + limit]\n"
    )
    (symbol,) = python_changeset(before, after).symbols
    assert (symbol.change, symbol.old_name, symbol.name) == (ChangeKind.RENAMED, "list_notes", "fetch_notes")


def test_dict_get_is_not_a_route() -> None:
    cs = python_changeset("", 'other = d.get("key")\nurl = os.environ.get("URL")\n')
    assert cs.routes == []


# --- JS / TS (regex) --------------------------------------------------------


def test_express_fixture() -> None:
    cs = build_changeset(load("express_app.diff"))
    assert symbol_changes(cs) == {
        "createUser": ChangeKind.SIGNATURE_CHANGED,
        "deleteUser": ChangeKind.ADDED,
    }
    routes = {(r.method, r.path): r.change for r in cs.routes}
    assert routes == {("DELETE", "/legacy"): ChangeKind.REMOVED, ("POST", "/users"): ChangeKind.ADDED}
    assert names(cs.env_vars, "name", ChangeKind.REMOVED) == {"PORT"}
    assert names(cs.env_vars, "name", ChangeKind.ADDED) == {"SERVER_PORT", "API_KEY"}


def test_js_body_change_detected_via_hunk_header() -> None:
    patch = "@@ -10,3 +10,3 @@ export function render(props) {\n-  return a;\n+  return b;"
    source = "export function render(props) {\n  return x;\n}\n"
    changed = ChangedFile(path="ui/view.js", patch=patch, before=source, after=source)
    assert symbol_changes(build_changeset([changed])) == {"render": ChangeKind.MODIFIED}


# --- CLI flags --------------------------------------------------------------


def test_cli_flag_fixture() -> None:
    cs = build_changeset(load("cli_flags.diff"))
    assert names(cs.cli_flags, "flag", ChangeKind.REMOVED) == {"--verbose"}
    assert names(cs.cli_flags, "flag", ChangeKind.ADDED) == {"--quiet", "--output-dir", "--max"}


def test_typer_implicit_flags_and_commander() -> None:
    before = "program.option('-p, --port <number>', 'port')\n"
    after = "def run(repo_name: str = typer.Option(...)):\n    pass\n"
    cs = build_changeset([ChangedFile(path="cli.ts", before=before, after="")])
    assert names(cs.cli_flags, "flag", ChangeKind.REMOVED) == {"--port"}
    cs = python_changeset("", after)
    assert names(cs.cli_flags, "flag", ChangeKind.ADDED) == {"--repo-name"}


# --- filtering --------------------------------------------------------------


def test_test_and_doc_files_are_skipped() -> None:
    files = [
        ChangedFile(path="tests/test_core.py", before="", after="def test_x():\n    pass\n"),
        ChangedFile(path="src/widget.test.ts", before="", after="function helper() {}\n"),
        ChangedFile(path="README.md", patch="@@ -1 +1 @@\n-old\n+def fake(): pass"),
    ]
    cs = build_changeset(files)
    assert cs.is_empty()
    assert cs.files == ["tests/test_core.py", "src/widget.test.ts", "README.md"]
