import ast
import sys

import pytest

from driftless.app_env import PLACEHOLDER, app_env, required_env_vars
from driftless.executable_docs import DocRunner
from driftless.execution import ExampleStatus
from driftless.sandbox import Sandbox, sandbox_env

# The live bug: the code's default changed from 1000 to 10, the docs still say 1000.
NOTES_APP = '''import os

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

MAX_BODY_LENGTH = int(os.getenv("MAX_BODY_LENGTH", "10"))

app = FastAPI()


class NoteIn(BaseModel):
    body: str


@app.post("/notes", status_code=201)
def create_note(note: NoteIn):
    if len(note.body) > MAX_BODY_LENGTH:
        raise HTTPException(status_code=422, detail="body too long")
    return {"id": 1, "body": note.body}
'''

NOTES_README = '''# Notes

| Variable | Default | Description |
|----------|---------|-------------|
| `MAX_BODY_LENGTH` | `1000` | Longest note body accepted. |

```bash
curl -X POST http://localhost:8000/notes -H "Content-Type: application/json" -d '{"body": "Buy milk and eggs today"}'
```

`201 Created`

```json
{"id": 1, "body": "Buy milk and eggs today"}
```
'''


def test_documented_default_does_not_override_the_code_default() -> None:
    env = app_env({"app/main.py": NOTES_APP}, {"README.md": NOTES_README})
    assert "MAX_BODY_LENGTH" not in env.set
    assert "MAX_BODY_LENGTH" in env.unset  # nor inherited from the host


def test_changed_code_default_makes_the_documented_example_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAX_BODY_LENGTH", "1000")  # even if the host happens to set it
    files = {"app/__init__.py": "", "app/main.py": NOTES_APP, "README.md": NOTES_README}
    report = DocRunner(Sandbox(files, python=sys.executable)).execute({"README.md": NOTES_README}, files)
    [outcome] = report.outcomes
    assert outcome.label == "POST /notes"
    assert outcome.status == ExampleStatus.FAILING
    assert (outcome.expected_status, outcome.actual_status) == (201, 422)


def test_only_variables_without_a_code_default_are_set() -> None:
    code = '''import os
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

SECRET_KEY = os.environ["SECRET_KEY"]
WORKERS = int(os.environ["WORKERS"])
PAGE = os.getenv("PAGE_SIZE", "20")
DEBUG = os.environ.get("DEBUG")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="APP_")
    database_url: str
    port: int = Field(...)
    ratio: float
    verbose: bool
    name: str = "notes"
    tags: list[str] = Field(default_factory=list)
    timeout: int = Field(default=5)
'''
    required = required_env_vars(ast.parse(code))
    assert required == {
        "SECRET_KEY": PLACEHOLDER,
        "WORKERS": "1",
        "APP_DATABASE_URL": PLACEHOLDER,
        "APP_PORT": "1",
        "APP_RATIO": "1.0",
        "APP_VERBOSE": "false",
    }

    docs = {"README.md": "| Name | Default |\n|---|---|\n| `APP_DATABASE_URL` | `memory://` |\n| `PAGE_SIZE` | `50` |\n"}
    env = app_env({"app/config.py": code}, docs)
    assert env.set["APP_DATABASE_URL"] == "memory://"  # required: the documented value beats a placeholder
    assert "PAGE_SIZE" not in env.set  # has a code default: the docs' 50 is never applied
    assert {"PAGE_SIZE", "DEBUG", "SECRET_KEY"} <= env.unset


def test_sandbox_env_drops_variables_the_app_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAX_BODY_LENGTH", "1000")
    env = sandbox_env({"SECRET_KEY": PLACEHOLDER}, unset={"MAX_BODY_LENGTH", "SECRET_KEY"})
    assert "MAX_BODY_LENGTH" not in env
    assert env["SECRET_KEY"] == PLACEHOLDER
