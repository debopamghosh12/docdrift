import hashlib
import hmac
import json
import threading
from typing import Any

from fastapi.testclient import TestClient

from docdrift.config import Settings
from docdrift.pipeline import OwnPullRequestError
from docdrift.pr_writer import PublishAction, PublishResult
from docdrift.server import create_app

SECRET = "s3cret"
DOCS_PR_URL = "https://github.com/me/app/pull/8"


class FakeSync:
    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[tuple[str, int]] = []
        self.error = error

    def __call__(self, repo: str, number: int) -> PublishResult:
        self.calls.append((repo, number))
        if self.error:
            raise self.error
        return PublishResult(action=PublishAction.OPENED, url=DOCS_PR_URL, branch=f"docdrift/pr-{number}")


def make_client(sync: FakeSync, secret: str = SECRET) -> TestClient:
    return TestClient(create_app(Settings(_env_file=None, webhook_secret=secret), sync=sync))


def sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def post(client: TestClient, event: str, payload: dict[str, Any], signature: str | None = None):
    body = json.dumps(payload).encode()
    headers = {
        "X-GitHub-Event": event,
        "X-Hub-Signature-256": signature or sign(body),
        "Content-Type": "application/json",
    }
    return client.post("/webhook", content=body, headers=headers)


def pr_event(action: str = "closed", merged: bool = True, head_ref: str = "feature/x") -> dict[str, Any]:
    return {
        "action": action,
        "repository": {"full_name": "me/app"},
        "pull_request": {"number": 7, "merged": merged, "head": {"ref": head_ref}},
    }


def test_valid_signature_is_accepted() -> None:
    response = post(make_client(FakeSync()), "ping", {"zen": "Keep it simple."})
    assert response.status_code == 200


def test_invalid_signature_is_rejected() -> None:
    sync = FakeSync()
    response = post(make_client(sync), "pull_request", pr_event(), signature=sign(b"other body"))
    assert response.status_code == 401
    assert sync.calls == []


def test_missing_signature_or_secret_is_rejected() -> None:
    client = make_client(FakeSync())
    response = client.post("/webhook", content=b"{}", headers={"X-GitHub-Event": "ping"})
    assert response.status_code == 401
    body = json.dumps({"zen": "x"}).encode()
    assert post(make_client(FakeSync(), secret=""), "ping", {"zen": "x"}, sign(body, "")).status_code == 401


def test_ping_returns_ok() -> None:
    response = post(make_client(FakeSync()), "ping", {"zen": "Design for failure."})
    assert response.json() == {"status": "ok", "zen": "Design for failure."}


def test_non_merged_pr_is_ignored() -> None:
    sync = FakeSync()
    client = make_client(sync)
    for payload in (pr_event(merged=False), pr_event(action="opened", merged=False)):
        response = post(client, "pull_request", payload)
        assert response.status_code == 200
        assert response.json()["status"] == "ignored"
    assert sync.calls == []
    assert client.get("/runs").json() == []


def test_docdrift_branch_is_ignored() -> None:
    sync = FakeSync()
    response = post(make_client(sync), "pull_request", pr_event(head_ref="docdrift/pr-3"))
    assert response.json()["status"] == "ignored"
    assert sync.calls == []


def test_other_events_are_ignored() -> None:
    response = post(make_client(FakeSync()), "issue_comment", {"action": "created"})
    assert response.json()["status"] == "ignored"


def test_merged_pr_queues_the_sync_job() -> None:
    sync = FakeSync()
    client = make_client(sync)
    response = post(client, "pull_request", pr_event())

    assert response.status_code == 202
    assert response.json()["status"] == "queued"
    assert sync.calls == [("me/app", 7)]  # TestClient runs background tasks before returning
    [run] = client.get("/runs").json()
    assert run["status"] == "succeeded"
    assert run["docs_pr_url"] == DOCS_PR_URL
    assert run["action"] == "opened"
    assert run["duration_s"] is not None


def test_failed_and_skipped_jobs_are_recorded() -> None:
    client = make_client(FakeSync(RuntimeError("boom")))
    post(client, "pull_request", pr_event())
    [run] = client.get("/runs").json()
    assert run["status"] == "failed"
    assert "boom" in run["error"]

    client = make_client(FakeSync(OwnPullRequestError("own PR")))
    post(client, "pull_request", pr_event())
    assert client.get("/runs").json()[0]["status"] == "skipped"


def test_same_pr_does_not_run_twice_concurrently() -> None:
    started, release = threading.Event(), threading.Event()

    def slow_sync(repo: str, number: int) -> PublishResult:
        started.set()
        release.wait(5)
        return PublishResult(action=PublishAction.UPDATED, url=DOCS_PR_URL)

    app = create_app(Settings(_env_file=None, webhook_secret=SECRET), sync=slow_sync)
    store = app.state.jobs
    job, created = store.enqueue("me/app", 7)
    worker = threading.Thread(target=store.run, args=(job,))
    worker.start()
    assert started.wait(5)

    response = post(TestClient(app), "pull_request", pr_event())
    assert response.status_code == 200
    assert response.json()["status"] == "already running"
    assert response.json()["job"]["id"] == job.id

    release.set()
    worker.join(5)
    assert store.enqueue("me/app", 7)[1] is True  # free again once finished


def test_runs_keeps_last_20_newest_first() -> None:
    client = make_client(FakeSync())
    for number in range(25):
        payload = pr_event()
        payload["pull_request"]["number"] = number
        post(client, "pull_request", payload)
    runs = client.get("/runs").json()
    assert len(runs) == 20
    assert runs[0]["pr"] == 24


def test_health() -> None:
    assert make_client(FakeSync()).get("/health").json() == {"status": "ok"}
