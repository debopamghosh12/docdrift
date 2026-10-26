from driftless.candidates import find_missing_docs, select_candidates
from driftless.doc_index import index_document
from driftless.models import (
    ChangeKind,
    ChangeSet,
    CliFlagChange,
    DocFile,
    EnvVarChange,
    RouteChange,
    SymbolChange,
    SymbolKind,
)

DOC = """\
# Tool

## Configuration

Set `API_URL` before starting. The client calls `fetch_user(user_id)`.

## API

`GET /users/{id}` returns a user.

## Running

You can run the tool with `tool sync --verbose`.

## About

We run a tight ship.
"""


def sections():
    return index_document(DocFile(path="README.md", content=DOC))


def changeset() -> ChangeSet:
    return ChangeSet(
        symbols=[
            SymbolChange(name="fetch_user", kind=SymbolKind.FUNCTION, change=ChangeKind.SIGNATURE_CHANGED, file="a.py"),
            SymbolChange(name="run", kind=SymbolKind.FUNCTION, change=ChangeKind.MODIFIED, file="a.py"),
            SymbolChange(name="brand_new", kind=SymbolKind.FUNCTION, change=ChangeKind.ADDED, file="a.py"),
        ],
        env_vars=[
            EnvVarChange(name="API_URL", change=ChangeKind.REMOVED, file="a.py"),
            EnvVarChange(name="NEW_TOKEN", change=ChangeKind.ADDED, file="a.py"),
        ],
        routes=[
            RouteChange(method="GET", path="/users/:id", change=ChangeKind.REMOVED, file="a.ts"),
            RouteChange(method="POST", path="/orders", change=ChangeKind.ADDED, file="a.ts"),
        ],
        cli_flags=[CliFlagChange(flag="--verbose", change=ChangeKind.REMOVED, file="cli.py")],
    )


def test_candidates_ranked_by_relevance() -> None:
    result = select_candidates(sections(), changeset())
    headings = [c.section.heading for c in result]
    assert headings[0] == "Configuration"
    assert set(headings) == {"Configuration", "API", "Running"}
    config = result[0]
    assert set(config.matched_terms) == {"API_URL", "fetch_user"}


def test_route_params_match_across_styles() -> None:
    api = next(c for c in select_candidates(sections(), changeset()) if c.section.heading == "API")
    assert set(api.matched_terms) == {"/users/:id", "GET /users/:id"}


def test_missing_route_requires_method_match() -> None:
    # The path is documented for GET, but the new PATCH route isn't.
    cs = ChangeSet(
        routes=[RouteChange(method="PATCH", path="/users/{id}", change=ChangeKind.ADDED, file="a.py")]
    )
    assert [m.name for m in find_missing_docs(sections(), cs)] == ["PATCH /users/{id}"]


def test_missing_skips_route_handlers_and_their_models_and_dedupes() -> None:
    cs = ChangeSet(
        symbols=[
            SymbolChange(
                name="update_user", kind=SymbolKind.FUNCTION, change=ChangeKind.ADDED, file="main.py",
                new_signature="update_user(id: int, patch: UserPatch)", route="PATCH /users/{id}",
            ),
            SymbolChange(name="UserPatch", kind=SymbolKind.CLASS, change=ChangeKind.ADDED, file="main.py"),
            SymbolChange(name="save", kind=SymbolKind.FUNCTION, change=ChangeKind.ADDED, file="db.py"),
            SymbolChange(name="save", kind=SymbolKind.FUNCTION, change=ChangeKind.ADDED, file="cache.py"),
        ]
    )
    assert [(m.name, m.file) for m in find_missing_docs(sections(), cs)] == [("save", "db.py")]


def test_generic_words_only_match_in_code() -> None:
    # "run" appears in prose under "About" but should not make it a candidate.
    headings = {c.section.heading for c in select_candidates(sections(), changeset())}
    assert "About" not in headings


def test_top_k_limits_results() -> None:
    assert len(select_candidates(sections(), changeset(), top_k=1)) == 1


def test_missing_docs() -> None:
    missing = {(m.kind, m.name) for m in find_missing_docs(sections(), changeset())}
    assert missing == {("function", "brand_new"), ("env_var", "NEW_TOKEN"), ("route", "POST /orders")}
