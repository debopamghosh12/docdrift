from docdrift.diff_parser import build_changeset
from docdrift.doc_index import index_document
from docdrift.evidence import best_example, describe_changeset, file_anchors, relevant_hunks
from docdrift.models import ChangedFile, DocFile

DOC = DocFile(
    path="README.md",
    content=(
        "# App\n\n## Endpoints\n\n### `GET /notes/{note_id}`\n\nFetch by `note_id`.\n\n"
        "## Service functions (`app/service.py`)\n\n### `get_note(note_id)`\n\nReturns a note.\n"
    ),
)
SERVICE = ChangedFile(
    path="app/service.py",
    before="def get_note(note_id):\n    return None\n",
    after="def get_note(note_id):\n    return None\n\ndef update_note(note_id, body=None):\n    return None\n",
    patch="@@ -2,0 +3,3 @@\n+def update_note(note_id, body=None):\n+    return None",
)


def test_sibling_anchors_pick_the_service_section_not_the_endpoint() -> None:
    sections = index_document(DOC)
    hunks = relevant_hunks([SERVICE], ["update_note"])
    anchors = file_anchors(SERVICE) - {"update_note"}
    assert anchors == {"app/service.py", "get_note"}
    example = best_example(sections, hunks, anchors)
    assert example is not None and example.heading == "`get_note(note_id)`"


def test_relevant_hunks_filters_by_term_and_falls_back_to_all() -> None:
    other = ChangedFile(path="b.py", patch="@@ -1 +1 @@\n-x = 1\n+x = 2")
    assert "b.py" not in relevant_hunks([SERVICE, other], ["update_note"])
    assert "b.py" in relevant_hunks([SERVICE, other], ["nothing_matches"])
    # Route terms match on their path.
    route_file = ChangedFile(path="main.py", patch='@@ -1 +1 @@\n+@app.patch("/notes/{note_id}")')
    assert "main.py" in relevant_hunks([route_file], ["PATCH /notes/{note_id}"])


def test_describe_changeset_mentions_each_change() -> None:
    text = describe_changeset(build_changeset([SERVICE]))
    assert "added function in app/service.py: update_note(note_id, body=None)" in text
