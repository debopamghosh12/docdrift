"""`driftless doctor`: is this machine (or runner) ready to run Driftless? Never prints secrets.

GitHub write permissions are probed without changing anything: each probe POSTs an empty body
to an endpoint that needs the permission. GitHub checks access before validating the body, so
422 (invalid body) means the token may write there, and 403/404 means it may not. That works
the same for classic tokens, fine-grained tokens and an Actions `GITHUB_TOKEN`.
"""

import os
import sys
from collections.abc import Callable
from dataclasses import dataclass

import httpx

from driftless.config import Settings

GITHUB_API = os.environ.get("GITHUB_API_URL", "https://api.github.com")
MIN_PYTHON = (3, 11)


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    fix: str = ""  # what to do when it fails
    required: bool = True


def python_check(version: tuple[int, int, int] | None = None) -> Check:
    major, minor, micro = version or sys.version_info[:3]
    ok = (major, minor) >= MIN_PYTHON
    return Check(
        "Python", ok, f"{major}.{minor}.{micro}", "Driftless needs Python 3.11 or newer: install it and reinstall Driftless."
    )


def env_checks(settings: Settings) -> list[Check]:
    """Whether each secret is set (never its value), plus the non-secret LLM settings in use."""
    return [
        Check(
            "GITHUB_TOKEN", bool(settings.github_token), "set" if settings.github_token else "not set",
            "Run `driftless init`, or export GITHUB_TOKEN (a token with access to the repo).",
        ),
        Check(
            "LLM_API_KEY", bool(settings.llm_api_key), "set" if settings.llm_api_key else "not set",
            "Run `driftless init`, or export LLM_API_KEY (e.g. a Groq key from https://console.groq.com/keys).",
        ),
        Check(
            "WEBHOOK_SECRET", bool(settings.webhook_secret), "set" if settings.webhook_secret else "not set",
            "Only needed for `driftless serve`: set WEBHOOK_SECRET to a random string.", required=False,
        ),
    ]


def llm_check(settings: Settings, list_models: Callable[[Settings], list[str]] | None = None) -> Check:
    """The endpoint answers with this key, and serves LLM_MODEL when it lists its models."""
    name = f"LLM endpoint ({settings.llm_base_url}, {settings.llm_model})"
    if not settings.llm_api_key:
        return Check(name, False, "skipped: LLM_API_KEY is not set", "Set LLM_API_KEY first.")
    try:
        models = (list_models or _list_models)(settings)
    except Exception as exc:  # noqa: BLE001 - every failure is reported, none is fatal
        return Check(name, False, _llm_error(exc), "Check LLM_API_KEY and LLM_BASE_URL.")
    if models and settings.llm_model not in models:
        return Check(name, False, f"answers, but doesn't serve {settings.llm_model}", "Set LLM_MODEL to a model it lists.")
    return Check(name, True, "answers" + (f", serves {settings.llm_model}" if models else ""))


def _list_models(settings: Settings) -> list[str]:
    import openai

    client = openai.OpenAI(base_url=settings.llm_base_url, api_key=settings.llm_api_key, timeout=20, max_retries=0)
    try:
        return [model.id for model in client.models.list()]
    except openai.NotFoundError:
        return []  # no /models route: the endpoint still answered


def _llm_error(exc: Exception) -> str:
    import openai

    if isinstance(exc, openai.AuthenticationError):
        return "the key was rejected (401)"
    if isinstance(exc, openai.APIConnectionError):
        return "could not connect"
    if isinstance(exc, openai.APIStatusError):
        return f"HTTP {exc.status_code}"
    return type(exc).__name__


@dataclass(frozen=True)
class Probe:
    permission: str  # as fine-grained tokens name it
    method: str
    path: str  # under /repos/{repo}; `{sha}` is the default branch's head
    purpose: str
    workflow_key: str | None  # its name under a workflow's `permissions:`; None if GITHUB_TOKEN can't have it
    required: bool = True


PROBES = (
    Probe("Contents", "POST", "/git/refs", "commits doc fixes and docs-PR branches", "contents"),
    Probe("Pull requests", "POST", "/pulls", "opens docs PRs", "pull-requests"),
    Probe("Issues", "POST", "/issues", "posts PR comments", "issues"),
    Probe("Commit statuses", "POST", "/statuses/{sha}", "sets the `Driftless / docs-in-sync` status", "statuses"),
    Probe("Webhooks", "GET", "/hooks", "lets `driftless serve` connect a webhook", None, required=False),
)


def github_checks(token: str, repo: str, http: httpx.Client | None = None) -> list[Check]:
    """Token validity, repo access, then one probe per permission Driftless uses."""
    client = http or httpx.Client(base_url=GITHUB_API, timeout=20)
    client.headers.update(
        {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    )
    base = f"/repos/{repo}"
    response = client.get(base)
    if response.status_code == 401:
        return [Check("GitHub token", False, "rejected (401)", "Create a new token and run `driftless init`.")]
    scopes = response.headers.get("X-OAuth-Scopes")
    token_kind = f"classic token, scopes: {scopes or 'none'}" if scopes is not None else "fine-grained or app token"
    if response.status_code != 200:
        return [
            Check("GitHub token", True, token_kind),
            Check(f"Access to {repo}", False, f"HTTP {response.status_code}", "Give the token access to this repository."),
        ]
    info = response.json()
    checks = [Check("GitHub token", True, token_kind), Check(f"Access to {repo}", True, _role(info))]
    head = client.get(f"{base}/commits/{info.get('default_branch', 'main')}", headers={"Accept": "application/vnd.github.sha"})
    readable = head.status_code == 200
    contents_read = Probe("Contents", "GET", "", "reads docs and code", "contents")
    checks.append(_probe_check(contents_read, "read", head.status_code, readable))
    sha = head.text.strip() if readable else "0" * 40
    for probe in PROBES:
        writes = probe.method == "POST"
        response = client.request(probe.method, base + probe.path.format(sha=sha), json={} if writes else None)
        granted = response.status_code == (422 if writes else 200)
        checks.append(_probe_check(probe, "write" if writes else "read", response.status_code, granted))
    return checks


def _probe_check(probe: Probe, level: str, status: int, granted: bool) -> Check:
    purpose = probe.purpose
    if status == 410:  # issues are turned off on the repo: PR comments still work
        granted, purpose = True, "issues are disabled on this repo; PR comments use Pull requests"
    detail = purpose if granted else f"{purpose} (HTTP {status})"
    return Check(f"{probe.permission}: {level}", granted, detail, _grant(probe, level), probe.required)


def _role(info: dict) -> str:
    permissions = info.get("permissions") or {}
    role = next((r for r in ("admin", "maintain", "push", "triage", "pull") if permissions.get(r)), None)
    visibility = "private" if info.get("private") else "public"
    return f"{visibility} repo" + (f", your role: {role}" if role else "")


def _grant(probe: Probe, level: str) -> str:
    fine_grained = "Read and write" if level == "write" else "Read-only"
    classic = "`admin:repo_hook`" if probe.workflow_key is None else "`repo`"
    workflow = (
        f"`permissions: {probe.workflow_key}: {level}` in the workflow"
        if probe.workflow_key
        else "not available to a workflow's GITHUB_TOKEN"
    )
    return f"Fine-grained token: {probe.permission} → {fine_grained} · classic token: {classic} scope · Actions: {workflow}"


def run_checks(settings: Settings, repo: str | None) -> list[Check]:
    checks = [python_check(), *env_checks(settings), llm_check(settings)]
    if repo and settings.github_token:
        checks += github_checks(settings.github_token, repo)
    return checks
