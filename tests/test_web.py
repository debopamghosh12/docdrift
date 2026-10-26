import threading
import time
from typing import Any

from fastapi.testclient import TestClient
from github import GithubException

from driftless.audit import AuditResult
from driftless.config import Settings
from driftless.execution import ExampleOutcome, ExampleStatus, ExecutionReport
from driftless.jobs import JobMode, JobStatus
from driftless.pr_writer import PublishAction, PublishResult
from driftless.progress import Stage, StageProgress
from driftless.server import create_app
from driftless.sync import PullAnalysis
from driftless.web.access import AllowedRepos, parse_repo
from driftless.web.github_browser import HookSummary, PullSummary, RepoSummary
from tests.test_pr_writer import proposals
from tests.test_premerge import open_pr
from tests.test_server import FakeSync

TOKEN = "ghp_supersecrettoken123"
LLM_KEY = "gsk_llmsecretkey456"
WEBHOOK_SECRET = "hooksecret789"
XSS = "<script>alert('x')</script>"
DOCS_PR_URL = "https://github.com/me/app/pull/8"


class FakeBrowser:
    def __init__(self) -> None:
        self.hooks_list: list[HookSummary] = []
        self.created: list[tuple[str, str, str]] = []
        self.hook_error: GithubException | None = None
        self.pull_list = [
            PullSummary(number=7, title=f"Open PR {XSS}", state="open", url="https://github.com/me/app/pull/7",
                        head_ref="feature/x", head_sha="b" * 40),
            PullSummary(number=6, title="Merged PR", state="merged", url="https://github.com/me/app/pull/6",
                        head_ref="feature/y", head_sha="c" * 40),
            PullSummary(number=5, title="Fork PR", state="open", url="https://github.com/me/app/pull/5",
                        head_ref="patch-1", head_sha="d" * 40, same_repo=False),
            PullSummary(number=4, title="docs: sync", state="open", url="https://github.com/me/app/pull/4",
                        head_ref="driftless/pr-3", head_sha="e" * 40),
        ]

    def repo(self, name: str) -> RepoSummary:
        return RepoSummary(full_name=name, description=f"Demo {XSS}", default_branch="main", url=f"https://github.com/{name}")

    def doc_files(self, name: str, ref: str) -> list[str]:
        return ["README.md", "docs/api.md"]

    def pulls(self, name: str, limit: int = 10) -> list[PullSummary]:
        return self.pull_list

    def pull(self, name: str, number: int) -> PullSummary:
        return next(p for p in self.pull_list if p.number == number)

    def hooks(self, name: str) -> list[HookSummary]:
        return self.hooks_list

    def create_hook(self, name: str, url: str, secret: str) -> HookSummary:
        if self.hook_error:
            raise self.hook_error
        self.created.append((name, url, secret))
        hook = HookSummary(id=1, host="smee.io", events=["pull_request", "issue_comment"], active=True, sends_json=True)
        self.hooks_list.append(hook)
        return hook

    def login(self) -> str:
        return "token-owner"


class WebSync(FakeSync):
    """FakeSync plus the web modes; check runs can block until released."""

    def __init__(self, report: ExecutionReport | None = None) -> None:
        super().__init__()
        self.report = report or failing_report()
        self.check_calls: list[tuple[str, str, bool]] = []
        self.analyze_calls: list[int] = []
        self.release: threading.Event | None = None
        self.error: Exception | None = None

    def check(self, repo: str, ref: str, fix: bool, progress: StageProgress) -> AuditResult:
        self.check_calls.append((repo, ref, fix))
        progress(Stage.FETCH, f"Downloading {repo}@{ref}...")
        if self.release is not None:
            progress(Stage.RUN, "Running examples in README.md...")
            self.release.wait(5)
        if self.error:
            raise self.error
        audit = AuditResult(repo=repo, ref=ref, report=self.report, originals={"README.md": "x"}, fix=fix)
        if fix:
            audit.published = PublishResult(action=PublishAction.OPENED, url=DOCS_PR_URL)
        return audit

    def analyze(self, repo: str, number: int, progress: StageProgress) -> PullAnalysis:
        self.analyze_calls.append(number)
        result = open_pr()
        p = proposals()
        p.execution = failing_report()
        return PullAnalysis(result, p, 0.5, pre_merge=True, created_at=time.time())


def failing_report(label: str = "POST /notes") -> ExecutionReport:
    return ExecutionReport(
        app="app.main:app",
        outcomes=[
            ExampleOutcome(file="README.md", label="GET /health", line=4, status=ExampleStatus.PASSED,
                           expected_status=200, actual_status=200, final_expected_status=200, final_actual_status=200),
            ExampleOutcome(file="README.md", label=label, line=12, status=ExampleStatus.FAILING,
                           expected_status=201, actual_status=422, final_expected_status=201, final_actual_status=422,
                           failures=["status 422, docs say 201"]),
        ],
    )


def make(sync: WebSync | None = None, browser: FakeBrowser | None = None, **settings: Any) -> TestClient:
    options: dict[str, Any] = dict(
        _env_file=None, webhook_secret=WEBHOOK_SECRET, database_url="sqlite://", allowed_repos="me/app",
        github_token=TOKEN, llm_api_key=LLM_KEY,
    )
    options.update(settings)
    app = create_app(Settings(**options), syncer=sync or WebSync(), browser=browser or FakeBrowser())
    return TestClient(app)


def job_id(html: str) -> str:
    return html.split('id="job-', 1)[1].split('"', 1)[0]


# Access ------------------------------------------------------------------------------------


def test_allowed_repos_rules() -> None:
    allowed = AllowedRepos(" me/app , Org/* ")
    assert allowed.allows("me/app") and allowed.allows("ME/APP")
    assert allowed.allows("org/anything") and not allowed.allows("me/other")
    assert allowed.exact == ["me/app"]
    assert not AllowedRepos("").allows("me/app")  # nothing configured: nothing allowed


def test_parse_repo_accepts_names_and_urls() -> None:
    assert parse_repo("me/app") == ("me", "app")
    assert parse_repo("https://github.com/me/app.git") == ("me", "app")
    assert parse_repo("me") is None and parse_repo("../etc/passwd") is None


def test_home_page_has_the_hero_and_the_enabled_repos() -> None:
    page = make().get("/")
    assert page.status_code == 200
    assert "Docs that never drift." in page.text and "Driftless runs them." in page.text
    assert 'href="/r/me/app"' in page.text


def test_open_redirects_to_the_dashboard_and_rejects_bad_names() -> None:
    client = make()
    response = client.get("/open", params={"repo": "https://github.com/me/app"}, follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/r/me/app"
    assert client.get("/open", params={"repo": "not a repo"}).status_code == 400


def test_allowed_repo_dashboard_renders_every_tab() -> None:
    client = make()
    for path in ("", "/pulls", "/memory", "/activity", "/setup"):
        page = client.get(f"/r/me/app{path}")
        assert page.status_code == 200, path
        assert "docs/api.md" in page.text and "Docs health" in page.text


def test_blocked_repo_gets_the_not_enabled_page() -> None:
    sync = WebSync()
    client = make(sync)
    for path in ("/r/someone/else", "/r/someone/else/pulls", "/r/someone/else/memory"):
        page = client.get(path)
        assert page.status_code == 403
        assert "not enabled on this instance" in page.text
    assert client.post("/r/someone/else/check", data={"ref": "main"}).status_code == 403
    assert client.post("/r/someone/else/memory/ignore", data={"value": "x"}).status_code == 403
    assert sync.check_calls == []


def test_wildcard_owner_rule_allows_all_of_its_repos() -> None:
    client = make(allowed_repos="me/*")
    assert client.get("/r/me/other").status_code == 200
    assert client.get("/r/you/app").status_code == 403


# Checks and job polling ---------------------------------------------------------------------


def test_run_check_starts_a_background_job_and_shows_the_results() -> None:
    sync = WebSync()
    client = make(sync)
    response = client.post("/r/me/app/check", data={"ref": "main"})
    assert response.status_code == 202
    assert sync.check_calls == [("me/app", "main", False)]

    page = client.get(f"/jobs/{job_id(response.text)}")
    assert page.status_code == 200
    assert "hx-trigger" not in page.text  # finished: polling stops
    assert "1/2" in page.text and "POST /notes" in page.text
    assert "🧪 passed" in page.text and "❌ still failing" in page.text
    assert "Fix broken examples (open PR)" in page.text
    # the health tab shows the latest check again
    assert "POST /notes" in client.get("/r/me/app").text


def test_job_polling_shows_the_live_stage_until_it_finishes() -> None:
    sync = WebSync()
    sync.release = threading.Event()
    client = make(sync)
    store = client.app.state.jobs
    job = store.enqueue("me/app", 0, JobMode.CHECK, ref="main").job
    worker = threading.Thread(target=store.run, args=(job,))
    worker.start()
    try:
        for _ in range(100):
            if job.stage == Stage.RUN:
                break
            time.sleep(0.02)
        page = client.get(f"/jobs/{job.id}")
        assert 'hx-trigger="every 1500ms"' in page.text
        assert "Running examples in README.md..." in page.text
        assert "Starting app" in page.text and "Running examples" in page.text
    finally:
        sync.release.set()
        worker.join(5)
    assert job.status == JobStatus.SUCCEEDED
    assert "hx-trigger" not in client.get(f"/jobs/{job.id}").text


def test_a_second_check_of_the_same_ref_reuses_the_running_one() -> None:
    sync = WebSync()
    client = make(sync)
    store = client.app.state.jobs
    running = store.enqueue("me/app", 0, JobMode.CHECK, ref="main").job  # queued, not run
    response = client.post("/r/me/app/check", data={"ref": "main"})
    assert response.status_code == 200 and job_id(response.text) == running.id
    assert "already in progress" in response.text
    assert sync.check_calls == []


def test_fix_examples_opens_a_pr_and_needs_the_llm() -> None:
    sync = WebSync()
    client = make(sync)
    response = client.post("/r/me/app/check", data={"ref": "main", "fix": "true"})
    assert response.status_code == 202 and sync.check_calls == [("me/app", "main", True)]
    page = client.get(f"/jobs/{job_id(response.text)}").text
    assert DOCS_PR_URL in page and "Opened a PR" in page

    no_llm = make(WebSync(), llm_api_key="")
    assert no_llm.post("/r/me/app/check", data={"ref": "main", "fix": "true"}).status_code == 409


def test_check_refuses_bad_refs_and_disabled_execution() -> None:
    sync = WebSync()
    assert make(sync).post("/r/me/app/check", data={"ref": "../x"}).status_code == 400
    assert make(sync, execute_examples=False).post("/r/me/app/check", data={"ref": "main"}).status_code == 409
    assert sync.check_calls == []


def test_htmx_notices_are_swapped_with_200() -> None:
    response = make().post("/r/me/app/check", data={"ref": "../x"}, headers={"HX-Request": "true"})
    assert response.status_code == 200 and "isn&#39;t a branch" in response.text


def test_jobs_of_blocked_repos_are_not_served() -> None:
    client = make()
    job = client.app.state.jobs.enqueue("someone/else", 0, JobMode.CHECK, ref="main").job
    assert client.get(f"/jobs/{job.id}").status_code == 404
    assert client.get("/jobs/nope").status_code == 404


def test_failed_job_errors_never_show_secrets() -> None:
    sync = WebSync()
    sync.error = RuntimeError(f"request failed with Authorization: Bearer {TOKEN} and key {LLM_KEY}")
    client = make(sync)
    response = client.post("/r/me/app/check", data={"ref": "main"})
    page = client.get(f"/jobs/{job_id(response.text)}").text
    assert "failed" in page and "[redacted]" in page
    for secret in (TOKEN, LLM_KEY, WEBHOOK_SECRET):
        assert secret not in page
        assert secret not in client.get("/r/me/app/activity").text
        assert secret not in client.get("/r/me/app/setup").text


# Pull requests -----------------------------------------------------------------------------


def test_pulls_tab_lists_open_and_merged_prs_escaped() -> None:
    page = make().get("/r/me/app/pulls").text
    assert "Open PR &lt;script&gt;alert(&#39;x&#39;)&lt;/script&gt;" in page
    assert XSS not in page
    assert "Merged PR" in page and "Driftless docs PR" in page


def test_analyze_runs_a_dry_run_and_renders_the_analysis() -> None:
    sync = WebSync()
    client = make(sync)
    response = client.post("/r/me/app/pulls/7/analyze")
    assert response.status_code == 202 and sync.analyze_calls == [7]
    page = client.get(f"/jobs/{job_id(response.text)}").text
    assert "Stale sections" in page and "🔁 fixed on retry" in page and "⚠️ dropped" in page
    assert "diff-add" in page and "NEW_VAR" in page  # the colored doc diff
    assert "Executed examples" in page and "POST /notes" in page
    assert "nothing was posted to GitHub" in page
    assert "Post pre-merge check" in client.get("/r/me/app/pulls/7").text


def test_publish_actions_queue_the_same_jobs_as_the_webhook() -> None:
    sync = WebSync()
    client = make(sync)
    assert client.post("/r/me/app/pulls/7/premerge").status_code == 202
    assert sync.pre_merge_calls == ["b" * 40]
    assert client.post("/r/me/app/pulls/6/docs-pr").status_code == 202
    assert sync.calls == [("me/app", 6)]
    response = client.post("/r/me/app/pulls/7/apply")
    assert response.status_code == 202 and sync.apply_calls == ["token-owner"]
    modes = {job.mode for job in client.app.state.jobs.recent("me/app")}
    assert {JobMode.PRE_MERGE, JobMode.POST_MERGE, JobMode.APPLY} <= modes


def test_publish_actions_are_refused_where_they_dont_apply() -> None:
    sync = WebSync()
    client = make(sync)
    assert client.post("/r/me/app/pulls/5/apply").status_code == 409  # fork
    assert client.post("/r/me/app/pulls/6/premerge").status_code == 409  # merged
    assert client.post("/r/me/app/pulls/7/docs-pr").status_code == 409  # still open
    assert client.post("/r/me/app/pulls/4/analyze").status_code == 409  # Driftless's own PR
    assert sync.apply_calls == [] and sync.pre_merge_calls == [] and sync.calls == []


# Memory ------------------------------------------------------------------------------------


def test_memory_add_and_remove_use_the_same_storage_as_commands() -> None:
    client = make()
    memory = client.app.state.memory
    page = client.post("/r/me/app/memory/ignore", data={"value": "`CHANGELOG.md`"})
    assert page.status_code == 200 and "will ignore" in page.text
    assert memory.ignore_rules("me/app") == ["CHANGELOG.md"]  # quotes dropped, like a comment
    assert "already ignored" in client.post("/r/me/app/memory/ignore", data={"value": "CHANGELOG.md"}).text

    client.post("/r/me/app/memory/style", data={"value": "use British spelling"})
    client.post("/r/me/app/memory/style", data={"value": "keep examples minimal"})
    assert memory.style_notes("me/app") == ["use British spelling", "keep examples minimal"]
    assert "Removed style preference #1" in client.post("/r/me/app/memory/unstyle", data={"value": "1"}).text
    assert memory.style_notes("me/app") == ["keep examples minimal"]

    client.post("/r/me/app/memory/unignore", data={"value": "CHANGELOG.md"})
    assert memory.ignore_rules("me/app") == []
    assert "Enter a value" in client.post("/r/me/app/memory/ignore", data={"value": "  "}).text
    assert "Enter a value" in client.post("/r/me/app/memory/apply", data={"value": "x"}).text


def test_memory_tab_shows_rules_escaped_and_feedback_counts() -> None:
    client = make()
    memory = client.app.state.memory
    memory.add_ignore("me/app", XSS, "web")
    memory.record_docs_pr("me/app", 8, 7, [])
    memory.resolve_docs_pr("me/app", 8, merged=True)
    page = client.get("/r/me/app/memory").text
    assert XSS not in page and "&lt;script&gt;" in page
    assert "merged (accepted)" in page


# Activity and setup ------------------------------------------------------------------------


def test_activity_rows_list_this_repos_jobs_only() -> None:
    client = make()
    client.post("/r/me/app/check", data={"ref": "main"})
    client.app.state.jobs.enqueue("other/repo", 3, JobMode.POST_MERGE)
    rows = client.get("/r/me/app/activity/rows").text
    assert "Docs health check" in rows and "1/2 examples passing" in rows
    assert "#3" not in rows
    assert 'hx-trigger="every 3s"' in client.get("/r/me/app/activity").text


def test_setup_shows_presence_of_settings_and_connects_the_webhook() -> None:
    browser = FakeBrowser()
    client = make(browser=browser)
    page = client.get("/r/me/app/setup").text
    assert "not connected" in page and TOKEN not in page and WEBHOOK_SECRET not in page

    created = client.post("/r/me/app/webhook", data={"url": "https://smee.io/abc123"})
    assert "Webhook created" in created.text and "connected" in created.text
    assert browser.created == [("me/app", "https://smee.io/abc123", WEBHOOK_SECRET)]
    assert "abc123" not in client.get("/r/me/app/setup").text  # only the host is shown


def test_webhook_errors_fall_back_to_the_manual_steps() -> None:
    browser = FakeBrowser()
    browser.hook_error = GithubException(403, {"message": "Resource not accessible by personal access token"}, None)
    client = make(browser=browser)
    page = client.post("/r/me/app/webhook", data={"url": "https://smee.io/abc"}).text
    assert "GitHub refused to create the webhook (403" in page
    assert "Manual setup" in page and "<details" in page and " open>" in page
    assert "Enter the public payload URL" in client.post("/r/me/app/webhook", data={"url": "ftp://x"}).text
    assert "Set WEBHOOK_SECRET" in make(webhook_secret="").post("/r/me/app/webhook", data={"url": "https://smee.io/a"}).text


def test_cross_site_posts_are_refused() -> None:
    sync = WebSync()
    client = make(sync)
    response = client.post("/r/me/app/check", data={"ref": "main"}, headers={"Origin": "https://evil.example"})
    assert response.status_code == 403 and sync.check_calls == []
    same = client.post("/r/me/app/check", data={"ref": "main"}, headers={"Origin": "http://testserver"})
    assert same.status_code == 202
