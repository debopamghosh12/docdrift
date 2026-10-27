"""Public demo mode: read-only for visitors, writes behind the admin login, job caps and rate limits."""

from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from driftless.config import Settings
from driftless.jobs import JobMode, JobRefused, JobStore
from driftless.web.demo import ADMIN_COOKIE, ADMIN_ONLY, AdminSessions, DemoGuard, RateLimiter, client_ip
from tests.test_server import FakeSync
from tests.test_web import FakeBrowser, WebSync, make

ADMIN = "admin-token-for-tests"
WRITES = [
    ("/r/me/app/pulls/7/premerge", {}),
    ("/r/me/app/pulls/7/apply", {}),
    ("/r/me/app/pulls/6/docs-pr", {}),
    ("/r/me/app/check", {"ref": "main", "fix": "true"}),
    ("/r/me/app/memory/ignore", {"value": "CHANGELOG.md"}),
    ("/r/me/app/webhook", {"url": "https://smee.io/abc"}),
]


def demo(sync: WebSync | None = None, browser: FakeBrowser | None = None, **settings: Any) -> TestClient:
    options: dict[str, Any] = dict(public_demo=True, admin_token=ADMIN)
    options.update(settings)
    # https: the session cookie is Secure, so the test client only sends it over https.
    return TestClient(make(sync, browser, **options).app, base_url="https://testserver")


def login(client: TestClient, token: str = ADMIN) -> Any:
    return client.post("/admin/login", data={"token": token}, follow_redirects=False)


def request(ip: str = "1.2.3.4", headers: dict[str, str] | None = None, cookie: str = "") -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    if cookie:
        raw.append((b"cookie", f"{ADMIN_COOKIE}={cookie}".encode()))
    return Request({"type": "http", "method": "POST", "path": "/", "headers": raw, "client": (ip, 1234)})


# Writes are admin-only -----------------------------------------------------------------------


@pytest.mark.parametrize(("path", "data"), WRITES)
def test_writes_to_github_are_refused_without_an_admin_session(path: str, data: dict[str, str]) -> None:
    sync, browser = WebSync(), FakeBrowser()
    client = demo(sync, browser)
    response = client.post(path, data=data)
    assert response.status_code == 403 and ADMIN_ONLY in response.text
    # htmx swaps only 2xx responses, so it gets the same notice with 200.
    assert client.post(path, data=data, headers={"HX-Request": "true"}).status_code == 200
    assert not (sync.pre_merge_calls or sync.apply_calls or sync.calls or sync.check_calls)
    assert client.app.state.memory.ignore_rules("me/app") == [] and browser.created == []


def test_visitors_can_run_checks_and_analyses() -> None:
    sync = WebSync()
    client = demo(sync)
    assert client.post("/r/me/app/check", data={"ref": "main"}).status_code == 202
    assert client.post("/r/me/app/pulls/7/analyze").status_code == 202
    assert sync.check_calls == [("me/app", "main", False)] and sync.analyze_calls == [7]


def test_write_buttons_are_disabled_with_a_tooltip_and_the_banner_shows() -> None:
    client = demo()
    page = client.get("/r/me/app/pulls/7").text
    assert page.count(f'title="{ADMIN_ONLY}"') == 2  # post pre-merge check, apply
    assert "Public demo" in page and "#use-as-a-github-action" in page and 'href="/admin"' in page
    assert 'hx-post="/r/me/app/pulls/7/analyze"' in page and "🔒 Analyze" not in page
    assert f'title="{ADMIN_ONLY}"' in client.get("/r/me/app/memory").text
    assert f'title="{ADMIN_ONLY}"' in client.get("/r/me/app/setup").text


def test_off_the_demo_nothing_is_locked() -> None:
    client = make()
    page = client.get("/r/me/app/pulls/7").text
    assert ADMIN_ONLY not in page and "Public demo" not in page
    assert client.post("/r/me/app/pulls/7/premerge").status_code == 202


# Admin login -----------------------------------------------------------------------------------


def test_admin_login_sets_a_hardened_cookie_and_unlocks_writes() -> None:
    sync = WebSync()
    client = demo(sync)
    response = login(client)
    assert response.status_code == 303
    cookie = response.headers["set-cookie"].lower()
    assert all(flag in cookie for flag in ("httponly", "secure", "samesite=lax", f"{ADMIN_COOKIE}="))

    page = client.get("/r/me/app/pulls/7").text
    assert "🔒" not in page and "Log out" in page
    assert client.post("/r/me/app/pulls/7/premerge").status_code == 202
    assert sync.pre_merge_calls == ["b" * 40]
    assert client.post("/r/me/app/memory/ignore", data={"value": "CHANGELOG.md"}).status_code == 200
    assert client.app.state.memory.ignore_rules("me/app") == ["CHANGELOG.md"]

    client.post("/admin/logout", follow_redirects=False)
    assert client.post("/r/me/app/pulls/7/apply").status_code == 403


def test_wrong_token_is_refused_and_failed_logins_are_rate_limited() -> None:
    client = demo()
    for _ in range(5):
        response = login(client, "wrong")
        assert response.status_code == 401 and ADMIN_COOKIE not in response.headers.get("set-cookie", "")
    assert login(client, "wrong").status_code == 429
    assert login(client).status_code == 429  # even the right token, until the window passes


def test_without_admin_token_nobody_can_log_in() -> None:
    client = demo(admin_token="")
    assert "is not set" in client.get("/admin").text
    assert login(client, "").status_code == 409
    assert client.post("/r/me/app/pulls/7/premerge").status_code == 403


def test_forged_and_expired_sessions_are_rejected() -> None:
    now = [1_000_000.0]
    sessions = AdminSessions(ADMIN, ttl_s=60, clock=lambda: now[0])
    cookie = sessions.issue()
    assert sessions.valid(cookie)
    expires, _, signature = cookie.partition(".")
    assert not sessions.valid(f"{int(expires) + 3600}.{signature}")  # extended expiry
    assert not sessions.valid(f"{expires}.{'0' * 64}") and not sessions.valid("garbage")
    assert not AdminSessions("another-token").valid(cookie)  # rotating the token logs everyone out
    now[0] += 61
    assert not sessions.valid(cookie)
    assert sessions.check_token(ADMIN) and not sessions.check_token(ADMIN + "x")
    assert not AdminSessions("").check_token("")


# Abuse limits ---------------------------------------------------------------------------------


def test_job_starts_are_rate_limited_per_ip() -> None:
    client = demo(demo_rate_limit=2)
    assert client.post("/r/me/app/check", data={"ref": "main"}).status_code == 202
    assert client.post("/r/me/app/check", data={"ref": "dev"}).status_code == 202
    limited = client.post("/r/me/app/check", data={"ref": "v1"})
    assert limited.status_code == 429 and "started 2 runs in the last 10 minutes" in limited.text

    login(client)  # admins aren't rate-limited
    assert client.post("/r/me/app/check", data={"ref": "v1"}).status_code == 202


def test_job_caps_per_repo_and_in_total() -> None:
    store = JobStore(FakeSync())
    guard = DemoGuard(Settings(_env_file=None, public_demo=True, admin_token=ADMIN))  # 1 per repo, 2 total
    visitor = request()
    store.enqueue("me/app", 0, JobMode.CHECK, ref="main", admit=guard.admit(visitor, "me/app"))
    # The same run again is shown, not refused; a different one for the same repo waits.
    assert not store.enqueue("me/app", 0, JobMode.CHECK, ref="main", admit=guard.admit(visitor, "me/app")).created
    with pytest.raises(JobRefused, match="one per repo"):
        store.enqueue("me/app", 0, JobMode.CHECK, ref="dev", admit=guard.admit(visitor, "me/app"))
    store.enqueue("me/other", 0, JobMode.CHECK, ref="main", admit=guard.admit(visitor, "me/other"))
    admin = request(cookie=guard.sessions.issue())
    with pytest.raises(JobRefused, match="already running 2 jobs"):  # caps apply to admins too
        store.enqueue("me/third", 0, JobMode.CHECK, ref="main", admit=guard.admit(admin, "me/third"))


def test_rate_limiter_window_slides() -> None:
    now = [0.0]
    limiter = RateLimiter(2, 600, clock=lambda: now[0])
    limiter.hit("a")
    limiter.hit("a")
    assert limiter.retry_after("a") == 600 and limiter.retry_after("b") == 0
    now[0] = 601
    assert limiter.retry_after("a") == 0


def test_client_ip_uses_the_proxys_entry() -> None:
    forwarded = request("10.0.0.1", {"X-Forwarded-For": "6.6.6.6, 203.0.113.9"})
    assert client_ip(forwarded, "X-Forwarded-For") == "203.0.113.9"  # a client can only prepend
    assert client_ip(forwarded) == "10.0.0.1"
    assert client_ip(request("10.0.0.1"), "X-Forwarded-For") == "10.0.0.1"
