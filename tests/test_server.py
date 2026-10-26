import hashlib
import hmac
import json
import threading
from collections.abc import Callable
from typing import Any

from fastapi.testclient import TestClient

from driftless.config import Settings
from driftless.jobs import JobMode
from driftless.pipeline import OwnPullRequestError
from driftless.pr_writer import PublishAction, PublishResult
from driftless.premerge import SupersededError
from driftless.server import create_app

SECRET = "s3cret"
DOCS_PR_URL = "https://github.com/me/app/pull/8"
COMMENT_URL = "https://github.com/me/app/pull/7#issuecomment-1"


class FakeSync:
    """Records every job the server runs; pre-merge runs can block until released."""

    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[tuple[str, int]] = []
        self.pre_merge_calls: list[str] = []
        self.apply_calls: list[str] = []
        self.error = error
        self.block: dict[str, threading.Event] = {}  # head sha -> released
        self.started: dict[str, threading.Event] = {}

    def post_merge(self, repo: str, number: int) -> PublishResult:
        self.calls.append((repo, number))
        if self.error:
            raise self.error
        return PublishResult(action=PublishAction.OPENED, url=DOCS_PR_URL, branch=f"driftless/pr-{number}")

    def pre_merge(self, repo: str, number: int, head_sha: str, should_stop: Callable[[], bool]) -> PublishResult:
        self.pre_merge_calls.append(head_sha)
        if head_sha in self.block:
            self.started[head_sha].set()
            self.block[head_sha].wait(5)
        if should_stop():
            raise SupersededError("newer push")
        return PublishResult(action=PublishAction.COMMENTED, url=COMMENT_URL, check_state="failure")

    def apply(self, repo: str, number: int, user: str) -> PublishResult:
        self.apply_calls.append(user)
        return PublishResult(action=PublishAction.COMMITTED, url="https://github.com/me/app/commit/abc")

    def hold(self, head_sha: str) -> threading.Event:
        """Make the pre-merge run of `head_sha` block until the returned event is set."""
        self.started[head_sha] = threading.Event()
        self.block[head_sha] = threading.Event()
        return self.block[head_sha]


def make_client(sync: FakeSync, secret: str = SECRET) -> TestClient:
    return TestClient(create_app(Settings(_env_file=None, webhook_secret=secret), syncer=sync))


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


def pr_event(
    action: str = "closed", merged: bool = True, head_ref: str = "feature/x", sha: str = "a1", draft: bool = False
) -> dict[str, Any]:
    return {
        "action": action,
        "repository": {"full_name": "me/app"},
        "pull_request": {"number": 7, "merged": merged, "draft": draft, "head": {"ref": head_ref, "sha": sha}},
    }


def comment_event(body: str = "/driftless apply", state: str = "open", on_pr: bool = True) -> dict[str, Any]:
    issue: dict[str, Any] = {"number": 7, "state": state}
    if on_pr:
        issue["pull_request"] = {"url": "https://api.github.com/repos/me/app/pulls/7"}
    return {
        "action": "created",
        "repository": {"full_name": "me/app"},
        "issue": issue,
        "comment": {"body": body, "user": {"login": "maintainer"}},
    }


def run_in_thread(store: Any, job: Any) -> threading.Thread:
    worker = threading.Thread(target=store.run, args=(job,))
    worker.start()
    return worker


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


def test_closed_without_merge_and_other_actions_are_ignored() -> None:
    sync = FakeSync()
    client = make_client(sync)
    for payload in (pr_event(merged=False), pr_event(action="labeled", merged=False)):
        response = post(client, "pull_request", payload)
        assert response.status_code == 200
        assert response.json()["status"] == "ignored"
    assert sync.calls == [] and sync.pre_merge_calls == []
    assert client.get("/runs").json() == []


def test_driftless_branch_is_ignored() -> None:
    sync = FakeSync()
    client = make_client(sync)
    for head_ref in ("driftless/pr-3", "docdrift/pr-3"):  # docdrift/ = branches from before the rename
        for action in ("closed", "opened", "synchronize"):
            assert post(client, "pull_request", pr_event(action, head_ref=head_ref)).json()["status"] == "ignored"
    assert sync.calls == [] and sync.pre_merge_calls == []


def test_other_events_are_ignored() -> None:
    response = post(make_client(FakeSync()), "push", {"ref": "refs/heads/main"})
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
    assert run["mode"] == "post-merge"
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

    class SlowSync(FakeSync):
        def post_merge(self, repo: str, number: int) -> PublishResult:
            started.set()
            release.wait(5)
            return PublishResult(action=PublishAction.UPDATED, url=DOCS_PR_URL)

    app = create_app(Settings(_env_file=None, webhook_secret=SECRET), syncer=SlowSync())
    store = app.state.jobs
    job = store.enqueue("me/app", 7).job
    worker = run_in_thread(store, job)
    assert started.wait(5)

    response = post(TestClient(app), "pull_request", pr_event())
    assert response.status_code == 200
    assert response.json()["status"] == "already running"
    assert response.json()["job"]["id"] == job.id

    release.set()
    worker.join(5)
    assert store.enqueue("me/app", 7).created is True  # free again once finished


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


# --- pre-merge mode ----------------------------------------------------------------------------


def test_pre_merge_actions_queue_a_check_of_the_head_commit() -> None:
    sync = FakeSync()
    client = make_client(sync)
    for i, action in enumerate(("opened", "synchronize", "reopened", "ready_for_review")):
        response = post(client, "pull_request", pr_event(action, merged=False, sha=f"sha{i}"))
        assert response.status_code == 202
    assert sync.pre_merge_calls == ["sha0", "sha1", "sha2", "sha3"]
    assert sync.calls == []
    run = client.get("/runs").json()[0]
    assert run["mode"] == "pre-merge"
    assert run["head_sha"] == "sha3"
    assert run["check_state"] == "failure"
    assert run["docs_pr_url"] == COMMENT_URL


def test_draft_pr_is_ignored() -> None:
    sync = FakeSync()
    client = make_client(sync)
    for action in ("opened", "synchronize"):
        response = post(client, "pull_request", pr_event(action, merged=False, draft=True))
        assert response.json() == {"status": "ignored", "reason": "draft pull request"}
    assert sync.pre_merge_calls == []
    assert client.get("/runs").json() == []


def test_runs_show_the_mode_of_each_job() -> None:
    client = make_client(FakeSync())
    post(client, "pull_request", pr_event("opened", merged=False))
    post(client, "issue_comment", comment_event())
    post(client, "pull_request", pr_event())
    assert [r["mode"] for r in client.get("/runs").json()] == ["post-merge", "apply", "pre-merge"]


def test_newer_push_supersedes_the_running_check() -> None:
    sync = FakeSync()
    release = sync.hold("old")
    client = make_client(sync)
    store = client.app.state.jobs
    old = store.enqueue("me/app", 7, JobMode.PRE_MERGE, head_sha="old").job
    worker = run_in_thread(store, old)
    assert sync.started["old"].wait(5)

    response = post(client, "pull_request", pr_event("synchronize", merged=False, sha="new"))
    assert response.status_code == 202
    assert response.json()["superseded"] == old.id
    release.set()
    worker.join(5)

    runs = {r["head_sha"]: r for r in client.get("/runs").json()}
    assert runs["old"]["status"] == "superseded"
    assert runs["old"]["superseded_by"] == runs["new"]["id"]
    assert runs["old"]["action"] is None  # it never commented
    assert runs["new"]["status"] == "succeeded"
    assert store.enqueue("me/app", 7, JobMode.PRE_MERGE, head_sha="newer").created  # slot is free


def test_redelivered_push_does_not_supersede_itself() -> None:
    sync = FakeSync()
    release = sync.hold("same")
    client = make_client(sync)
    store = client.app.state.jobs
    job = store.enqueue("me/app", 7, JobMode.PRE_MERGE, head_sha="same").job
    worker = run_in_thread(store, job)
    assert sync.started["same"].wait(5)

    response = post(client, "pull_request", pr_event("synchronize", merged=False, sha="same"))
    assert response.json()["status"] == "already running"
    release.set()
    worker.join(5)
    assert job.status == "succeeded"


def test_merge_supersedes_a_running_check_but_not_a_running_sync() -> None:
    store = make_client(FakeSync()).app.state.jobs
    check = store.enqueue("me/app", 7, JobMode.PRE_MERGE, head_sha="a1").job
    merged = store.enqueue("me/app", 7, JobMode.POST_MERGE)
    assert merged.created and merged.superseded is check and check.superseded

    again = store.enqueue("me/app", 7, JobMode.PRE_MERGE, head_sha="b2")
    assert not again.created and again.job is merged.job


def test_job_superseded_before_it_starts_never_runs() -> None:
    sync = FakeSync()
    store = make_client(sync).app.state.jobs
    old = store.enqueue("me/app", 7, JobMode.PRE_MERGE, head_sha="old").job
    store.enqueue("me/app", 7, JobMode.PRE_MERGE, head_sha="new")
    store.run(old)
    assert old.status == "superseded"
    assert sync.pre_merge_calls == []


# --- /driftless apply --------------------------------------------------------------------------


def test_apply_comment_queues_an_apply_job() -> None:
    sync = FakeSync()
    client = make_client(sync)
    response = post(client, "issue_comment", comment_event("  /driftless apply \n"))
    assert response.status_code == 202
    assert sync.apply_calls == ["maintainer"]
    [run] = client.get("/runs").json()
    assert (run["mode"], run["action"], run["requested_by"]) == ("apply", "committed", "maintainer")


def test_other_comments_are_ignored() -> None:
    sync = FakeSync()
    client = make_client(sync)
    for payload in (
        comment_event("/driftless apply please"),
        comment_event("LGTM"),
        comment_event(on_pr=False),
        comment_event(state="closed"),
        {**comment_event(), "action": "edited"},
    ):
        assert post(client, "issue_comment", payload).json()["status"] == "ignored"
    assert sync.apply_calls == []


def test_apply_does_not_supersede_a_running_check() -> None:
    store = make_client(FakeSync()).app.state.jobs
    check = store.enqueue("me/app", 7, JobMode.PRE_MERGE, head_sha="a1").job
    apply = store.enqueue("me/app", 7, JobMode.APPLY, requested_by="maintainer")
    assert apply.created and apply.superseded is None and not check.superseded
