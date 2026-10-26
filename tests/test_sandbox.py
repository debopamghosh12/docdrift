import sys
from pathlib import Path

import httpx
import pytest

from driftless.sandbox import Sandbox, SandboxError, find_fastapi_app, sandbox_env

FIXTURE = Path(__file__).parent / "fixtures" / "example_app"


def fixture_files() -> dict[str, str]:
    return {p.relative_to(FIXTURE).as_posix(): p.read_text(encoding="utf-8") for p in FIXTURE.rglob("*") if p.suffix in (".py", ".md", ".txt")}


def test_finds_the_fastapi_app_object() -> None:
    files = {
        "app/main.py": "from fastapi import FastAPI\napi = FastAPI(title='x')\n",
        "tools/admin.py": "import fastapi\nadmin = fastapi.FastAPI()\n",
        "tests/test_app.py": "from fastapi import FastAPI\napp = FastAPI()\n",
    }
    target = find_fastapi_app(files)
    assert target is not None and target.spec == "app.main:api" and target.app_dir == ""
    src = find_fastapi_app({"src/pkg/server.py": "from fastapi import FastAPI\napp = FastAPI()\n"})
    assert src is not None and src.spec == "pkg.server:app" and src.app_dir == "src"
    assert find_fastapi_app({"app.py": "from flask import Flask\napp = Flask(__name__)\n"}) is None


def test_secrets_are_not_passed_to_the_app(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    monkeypatch.setenv("LLM_API_KEY", "y")
    env = sandbox_env({"APP_PORT": "8000"})
    assert "GITHUB_TOKEN" not in env and "LLM_API_KEY" not in env
    assert env["APP_PORT"] == "8000" and "PATH" in {k.upper() for k in env}


def test_serves_a_fresh_app_and_kills_it_afterwards() -> None:
    with Sandbox(fixture_files(), python=sys.executable) as box:
        with box.serve({"GREETING": "hi"}) as base_url:
            assert httpx.get(f"{base_url}/greeting").json() == {"greeting": "hi"}
            assert httpx.post(f"{base_url}/items", json={"name": "Pen", "price": 1}).status_code == 201
        with pytest.raises(httpx.HTTPError):
            httpx.get(f"{base_url}/greeting", timeout=1)
        with box.serve() as fresh:
            assert httpx.get(f"{fresh}/items/1").status_code == 404  # state does not leak


def test_startup_failure_reports_the_log() -> None:
    files = {"app/main.py": "from fastapi import FastAPI\nraise RuntimeError('boom')\napp = FastAPI()\n"}
    with Sandbox(files, python=sys.executable, startup_timeout=15) as box:
        with pytest.raises(SandboxError, match="boom"):
            with box.serve():
                pass


def test_no_app_is_an_error() -> None:
    with pytest.raises(SandboxError, match="no FastAPI app"):
        with Sandbox({"main.py": "print('hi')\n"}, python=sys.executable):
            pass
