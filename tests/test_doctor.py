"""`driftless doctor` and `driftless init`: checks never leak secrets; .env is merged, not clobbered."""

import json
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from driftless.cli import app
from driftless.config import Settings
from driftless.doctor import env_checks, github_checks, llm_check, python_check
from driftless.init_env import merge_env

SECRET = "ghp_supersecretvalue123"
REPO = "acme/notes"


def test_python_version_gate() -> None:
    assert python_check((3, 11, 4)).ok
    assert not python_check((3, 10, 12)).ok


def test_env_checks_report_presence_never_values() -> None:
    checks = env_checks(Settings(_env_file=None, github_token=SECRET, llm_api_key="", webhook_secret=""))
    by_name = {c.name: c for c in checks}
    assert by_name["GITHUB_TOKEN"].ok and by_name["GITHUB_TOKEN"].detail == "set"
    assert not by_name["LLM_API_KEY"].ok and "driftless init" in by_name["LLM_API_KEY"].fix
    assert not by_name["WEBHOOK_SECRET"].required
    assert SECRET not in repr(checks)


def test_llm_check_requires_the_configured_model() -> None:
    settings = Settings(_env_file=None, llm_api_key="k", llm_model="openai/gpt-oss-120b")
    assert llm_check(settings, lambda s: ["openai/gpt-oss-120b", "x"]).ok
    assert not llm_check(settings, lambda s: ["other"]).ok
    assert llm_check(settings, lambda s: []).ok  # no model list: it answered


def test_llm_check_reports_errors_without_raising() -> None:
    def boom(settings: Settings) -> list[str]:
        raise TimeoutError

    check = llm_check(Settings(_env_file=None, llm_api_key="k"), boom)
    assert not check.ok and check.detail == "TimeoutError"


def github(status_for: dict[tuple[str, str], int], scopes: str | None = None) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == f"Bearer {SECRET}"
        if request.method == "POST":
            assert json.loads(request.content) == {}  # probes never send anything that could be created
        key = (request.method, request.url.path)
        if key == ("GET", f"/repos/{REPO}"):
            headers = {"X-OAuth-Scopes": scopes} if scopes is not None else {}
            body = {"default_branch": "main", "private": False, "permissions": {"admin": False, "push": True}}
            return httpx.Response(status_for.get(key, 200), json=body, headers=headers)
        if key == ("GET", f"/repos/{REPO}/commits/main"):
            return httpx.Response(status_for.get(key, 200), text="a" * 40)
        return httpx.Response(status_for.get(key, 403), json={"message": "x"})

    return httpx.Client(base_url="https://api.github.com", transport=httpx.MockTransport(handler))


def test_probes_read_422_as_granted_and_403_as_missing() -> None:
    statuses = {
        ("POST", f"/repos/{REPO}/git/refs"): 422,
        ("POST", f"/repos/{REPO}/pulls"): 422,
        ("POST", f"/repos/{REPO}/issues"): 410,  # issues disabled
        ("POST", f"/repos/{REPO}/statuses/{'a' * 40}"): 403,
        ("GET", f"/repos/{REPO}/hooks"): 404,
    }
    checks = {c.name: c for c in github_checks(SECRET, REPO, github(statuses))}
    assert checks[f"Access to {REPO}"].detail == "public repo, your role: push"
    assert checks["Contents: read"].ok
    assert checks["Contents: write"].ok and checks["Pull requests: write"].ok
    assert checks["Issues: write"].ok and "disabled" in checks["Issues: write"].detail
    status = checks["Commit statuses: write"]
    assert not status.ok and "permissions: statuses: write" in status.fix
    assert not checks["Webhooks: read"].ok and not checks["Webhooks: read"].required
    assert SECRET not in repr(checks)


def test_classic_token_scopes_are_shown() -> None:
    checks = github_checks(SECRET, REPO, github({}, scopes="repo, workflow"))
    assert checks[0].detail == "classic token, scopes: repo, workflow"


def test_no_repo_access_stops_early() -> None:
    checks = github_checks(SECRET, REPO, github({("GET", f"/repos/{REPO}"): 404}))
    assert [c.ok for c in checks] == [True, False]


def test_rejected_token() -> None:
    (check,) = github_checks(SECRET, REPO, github({("GET", f"/repos/{REPO}"): 401}))
    assert not check.ok and "401" in check.detail


def test_merge_env_replaces_in_place_and_appends() -> None:
    existing = "# mine\nGITHUB_TOKEN=old\nLLM_MODEL=custom\n"
    merged = merge_env(existing, {"GITHUB_TOKEN": "new", "LLM_API_KEY": "key"})
    assert merged == "# mine\nGITHUB_TOKEN=new\nLLM_MODEL=custom\n\nLLM_API_KEY=key\n"


def test_init_writes_env_and_keeps_existing_values(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("LLM_API_KEY=keepme\n", encoding="utf-8")
    result = CliRunner().invoke(app, ["init"], input=f"{SECRET}\n\n")
    assert result.exit_code == 0, result.output
    assert (tmp_path / ".env").read_text(encoding="utf-8") == f"LLM_API_KEY=keepme\n\nGITHUB_TOKEN={SECRET}\n"
    assert SECRET not in result.output
    assert ".gitignore" in result.output
