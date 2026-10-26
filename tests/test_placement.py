from driftless.code_index import build_code_index
from driftless.doc_index import index_documents
from driftless.models import ChangeKind, ChangeSet, DocFile, EnvVarChange, MissingDoc, SourceFile
from driftless.placement import covering_files

CODE = build_code_index(
    [
        SourceFile(
            path="app/main.py",
            content=(
                'import os\nPORT = os.getenv("APP_PORT")\nDB = os.getenv("DATABASE_URL")\n'
                'SIZE = os.getenv("NOTES_PAGE_SIZE")\n\n'
                '@app.get("/notes")\ndef a():\n    pass\n\n@app.delete("/notes/{id}")\ndef b(id):\n    pass\n\n'
                '@app.patch("/notes/{id}")\ndef c(id):\n    pass\n'
            ),
        )
    ]
)
README = DocFile(
    path="README.md",
    content="# App\n\n## Configuration\n\n| `APP_PORT` | x |\n| `DATABASE_URL` | y |\n\n"
    "## Endpoints\n\n### `GET /notes`\n\n### `DELETE /notes/{id}`\n",
)
API = DocFile(
    path="docs/api.md",
    content="# API\n\nBase URL uses `APP_PORT`.\n\n## Note object\n\nAt most `MAX_NOTE_LENGTH`.\n\n"
    "## List\n\n`GET /notes`\n\n## Delete\n\n`DELETE /notes/{id}`\n",
)
DOCS = [README, API]
CHANGES = ChangeSet(env_vars=[EnvVarChange(name="MAX_NOTE_LENGTH", change=ChangeKind.REMOVED, file="app/main.py")])


def covering(item: MissingDoc) -> list[str]:
    return covering_files(item, DOCS, index_documents(DOCS), CODE, CHANGES, [])


def test_routes_go_to_every_file_documenting_routes() -> None:
    item = MissingDoc(kind="route", name="PATCH /notes/{id}", file="app/main.py")
    assert covering(item) == ["README.md", "docs/api.md"]


def test_env_vars_only_go_where_one_section_lists_them() -> None:
    # docs/api.md mentions two env vars, but in different sections: not an env var reference.
    item = MissingDoc(kind="env_var", name="NOTES_PAGE_SIZE", file="app/main.py")
    assert covering(item) == ["README.md"]
