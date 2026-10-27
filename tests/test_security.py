"""Security fixes: whose code runs, sandbox isolation, archive limits, comment trust, web hardening."""

import io
import os
import re
import tarfile
from pathlib import Path
from typing import Any

import pytest

import driftless.analyzer
import driftless.executable_docs
import driftless.github_client
import driftless.pipeline
from driftless.audit import run_audit
from driftless.config import Settings
from driftless.github_writer import GitHubWriter
from driftless.isolation import IsolationError, clean_env, safe_requirements
from driftless.pr_writer import CommentRef
from driftless.premerge import PreMergeChecker
from driftless.premerge_body import fixes_marker, premerge_marker
from driftless.proposals import DocProposals
from driftless.sandbox import Sandbox, SandboxError, ensure_venv, safe_relative_path
from driftless.snapshot import SnapshotError, download_snapshot, read_tarball
from driftless.sync import analyze_and_propose
from driftless.trust import UntrustedCode, pull_execution_refusal, resolve_trusted_ref, valid_ref, valid_repo_name
from driftless.web.access import redact
from tests.test_demo import ADMIN, demo, login
from tests.test_premerge import HEAD, FakeCheckRepo, open_pr
from tests.test_pr_writer import REPO, result
from tests.test_web import WEBHOOK_SECRET, WebSync, make

FORK_SHA = "f" * 40
MAIN_SHA = "a" * 40


# Whose code runs ---------------------------------------------------------------------------


def test_merged_prs_run_their_merge_commit() -> None:
    assert pull_execution_refusal(result().pr, lambda repo, user: "none") is None


def test_fork_prs_never_run_whoever_asks() -> None:
    pr = open_pr(head_repo="mallory/app").pr
    refusal = pull_execution_refusal(pr, lambda repo, user: "admin")
    assert refusal is not None and "fork" in refusal
    deleted_fork = open_pr(head_repo="").pr
    assert pull_execution_refusal(deleted_fork, lambda repo, user: "admin") is not None


def test_same_repo_prs_run_only_for_authors_with_write_access() -> None:
    pr = open_pr(author="alice").pr
    assert pull_execution_refusal(pr, lambda repo, user: "write") is None
    assert pull_execution_refusal(pr, lambda repo, user: "maintain") is None
    assert "write access" in (pull_execution_refusal(pr, lambda repo, user: "read") or "")

    def broken(repo: str, user: str) -> str:
        raise RuntimeError("API down")

    assert pull_execution_refusal(pr, broken) is not None  # fails closed


class FakeRefs:
    def __init__(self) -> None:
        self.branches = {"main": MAIN_SHA, "feature/x": "b" * 40}
        self.tags = {"v1.0": "c" * 40}
        self.default_history = {MAIN_SHA, "d" * 40}

    def branch_sha(self, repo: str, branch: str) -> str | None:
        return self.branches.get(branch)

    def tag_sha(self, repo: str, tag: str) -> str | None:
        return self.tags.get(tag)

    def on_default_branch(self, repo: str, sha: str) -> str | None:
        return next((s for s in self.default_history if s.startswith(sha)), None)


def test_audits_run_branches_tags_and_default_branch_commits() -> None:
    refs = FakeRefs()
    assert resolve_trusted_ref(refs, REPO, "main") == MAIN_SHA
    assert resolve_trusted_ref(refs, REPO, "feature/x") == "b" * 40
    assert resolve_trusted_ref(refs, REPO, "v1.0") == "c" * 40
    assert resolve_trusted_ref(refs, REPO, "d" * 12) == "d" * 40


@pytest.mark.parametrize("ref", [FORK_SHA, "refs/pull/5/head", "pull/5/head", "no-such-branch"])
def test_audits_refuse_fork_commits_and_pull_refs(ref: str) -> None:
    with pytest.raises(UntrustedCode):
        resolve_trusted_ref(FakeRefs(), REPO, ref)


@pytest.mark.parametrize("ref", ["../../users", "a..b", "-x", "/main", "main/", "a//b", "x@{1}", "a b", "x.lock", ""])
def test_refs_that_could_escape_the_api_path_are_invalid(ref: str) -> None:
    assert not valid_ref(ref)


def test_repo_names_are_validated() -> None:
    assert valid_repo_name("me/app") and valid_repo_name("Org.x/my_app-2")
    for bad in ("me", "me/app/x", "../app", "me/..", "me/a b", "me/app?x=1"):
        assert not valid_repo_name(bad)


def test_run_audit_never_downloads_a_fork_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    downloads: list[str] = []

    class Client(FakeRefs):
        def __init__(self, token: str | None = None) -> None:
            super().__init__()

        def fetch_docs(self, repo: str, ref: str) -> list[Any]:
            downloads.append(ref)
            return []

    monkeypatch.setattr(driftless.github_client, "GitHubClient", Client)
    settings = Settings(_env_file=None, database_url="sqlite://")
    with pytest.raises(UntrustedCode):
        run_audit(settings, REPO, FORK_SHA)
    assert downloads == []


class PermissionClient:
    def __init__(self, permission: str) -> None:
        self._permission = permission

    def permission(self, repo: str, user: str) -> str:
        return self._permission

    def fetch_files(self, repo: str, ref: str) -> dict[str, str]:
        raise AssertionError("downloaded code it must not run")


class NoLLMAnalyzer:
    def __init__(self, llm: Any) -> None:
        pass

    def propose(self, result: Any, min_confidence: float) -> DocProposals:
        return DocProposals()


@pytest.mark.parametrize(("head_repo", "permission"), [("mallory/app", "admin"), (REPO, "read")])
def test_analyze_of_an_untrusted_pr_never_starts_the_sandbox(
    monkeypatch: pytest.MonkeyPatch, head_repo: str, permission: str
) -> None:
    """The web app's Analyze (any visitor) and webhook pre-merge runs both go through here."""
    monkeypatch.setattr(driftless.pipeline, "analyze_pull_request", lambda *a, **k: open_pr(head_repo=head_repo))
    monkeypatch.setattr(driftless.analyzer, "Analyzer", NoLLMAnalyzer)

    def never(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("ran the PR's code")

    monkeypatch.setattr(driftless.executable_docs, "execute_all_docs", never)
    settings = Settings(_env_file=None, database_url="sqlite://", execute_examples=True)
    _, proposals = analyze_and_propose(settings, PermissionClient(permission), None, REPO, 7, pre_merge=True)  # type: ignore[arg-type]
    assert proposals.execution is not None and proposals.execution.outcomes == []
    assert proposals.execution.errors[0].startswith("Examples were not run")


# Sandbox isolation -------------------------------------------------------------------------


def test_the_app_gets_an_allow_listed_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in ("GITHUB_TOKEN", "LLM_API_KEY", "ADMIN_TOKEN", "WEBHOOK_SECRET", "AWS_ACCESS_KEY_ID", "DATABASE_URL", "MY_THING"):
        monkeypatch.setenv(name, "secret-value")
    env = clean_env({"APP_MODE": "test"}, home=tmp_path)
    assert "secret-value" not in env.values()
    assert env["APP_MODE"] == "test" and env["HOME"] == str(tmp_path) and "PATH" in {k.upper() for k in env}


@pytest.mark.parametrize(
    "line",
    [
        "-r /proc/1/environ",
        "--index-url https://evil.example/simple",
        "-e .",
        ".",
        "./pkg",
        "/abs/path",
        "git+https://github.com/x/y",
        "pkg @ https://evil.example/pkg.tar.gz",
        "https://evil.example/pkg.whl",
    ],
)
def test_requirements_with_options_urls_or_paths_are_refused(line: str) -> None:
    with pytest.raises(IsolationError):
        safe_requirements(f"fastapi\n{line}\n")


def test_plain_requirements_are_kept() -> None:
    text = "# deps\nfastapi==0.110.0\nuvicorn[standard]>=0.29,<1  # server\npydantic ~= 2.6\n\ntyping-extensions; python_version < '3.12'\n"
    assert safe_requirements(text).splitlines() == [
        "fastapi==0.110.0", "uvicorn[standard]>=0.29,<1", "pydantic ~= 2.6", "typing-extensions; python_version < '3.12'",
    ]


def test_bad_requirements_fail_before_anything_is_installed(tmp_path: Path) -> None:
    with pytest.raises(SandboxError, match="unsupported line"):
        ensure_venv("-r /proc/1/environ\n", tmp_path)
    assert not (tmp_path / "venvs").exists()


@pytest.mark.parametrize("path", ["../escape.py", "/etc/x.py", "a/../../x.py", "C:/x.py", "a\\..\\x.py", ""])
def test_snapshot_paths_outside_the_sandbox_are_never_written(path: str, tmp_path: Path) -> None:
    assert not safe_relative_path(path)
    box = Sandbox({path: "x = 1\n", "ok/main.py": "y = 2\n"}, python="python")
    box._write_files(tmp_path)  # noqa: SLF001
    written = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*") if p.is_file())
    assert written == ["ok/main.py"]


@pytest.mark.skipif(os.name == "nt", reason="SANDBOX_USER is Linux-only")
def test_sandbox_user_requires_root() -> None:
    from driftless.isolation import sandbox_user

    if os.geteuid() == 0:
        pytest.skip("running as root")
    with pytest.raises(IsolationError, match="root"):
        sandbox_user("nobody")


@pytest.mark.skipif(os.name == "nt" or not hasattr(os, "geteuid") or os.geteuid() != 0, reason="needs root on Linux")
def test_locking_a_venv_never_follows_links_out_of_it(tmp_path: Path) -> None:
    import pwd

    from driftless.isolation import SandboxUser

    nobody = pwd.getpwnam("nobody")
    user = SandboxUser("nobody", nobody.pw_uid, nobody.pw_gid)
    outside = tmp_path / "outside.txt"
    outside.write_text("root's file")
    os.chmod(outside, 0o600)
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").write_text("#!")
    (venv / "bin" / "link").symlink_to(outside)
    user.give(venv)
    user.lock(venv)
    assert outside.stat().st_mode & 0o777 == 0o600 and outside.stat().st_uid == 0  # untouched
    assert (venv / "bin" / "python").stat().st_uid == 0 and not (venv / "bin" / "python").stat().st_mode & 0o022
    os.link(outside, venv / "bin" / "hard")  # a foreign file hardlinked in
    os.chown(venv / "bin", nobody.pw_uid, nobody.pw_gid)
    os.chown(outside, 1234, 1234)
    with pytest.raises(IsolationError):
        user.lock(venv)


# Archives ----------------------------------------------------------------------------------


def tarball(members: list[tarfile.TarInfo], data: bytes = b"x = 1\n") -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for info in members:
            archive.addfile(info, io.BytesIO(data) if info.isfile() else None)
    return buffer.getvalue()


def member(name: str, kind: bytes = tarfile.REGTYPE, size: int = 6, link: str = "") -> tarfile.TarInfo:
    info = tarfile.TarInfo(f"o-r-abc/{name}")
    info.type, info.size, info.linkname = kind, size if kind == tarfile.REGTYPE else 0, link
    return info


def test_tar_slip_links_and_devices_are_skipped() -> None:
    data = tarball([
        member("../../etc/evil.py"),
        member("/abs.py"),
        member("docs/link.md", tarfile.SYMTYPE, link="/etc/passwd"),
        member("app/hard.py", tarfile.LNKTYPE, link="o-r-abc/app/main.py"),
        member("app/dev.py", tarfile.CHRTYPE),
        member("app/main.py"),
    ])
    assert read_tarball(data) == {"app/main.py": "x = 1\n"}


def test_archives_with_too_many_members_or_bytes_are_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    import driftless.snapshot

    data = tarball([member(f"app/m{i}.py") for i in range(5)])
    monkeypatch.setattr(driftless.snapshot, "MAX_MEMBERS", 3)
    with pytest.raises(SnapshotError, match="more than 3 files"):
        read_tarball(data)
    monkeypatch.setattr(driftless.snapshot, "MAX_MEMBERS", 100)
    monkeypatch.setattr(driftless.snapshot, "MAX_TOTAL_BYTES", 10)
    with pytest.raises(SnapshotError, match="exceed"):
        read_tarball(data)


@pytest.mark.parametrize(("repo", "ref"), [("me/app", "../../user"), ("me/../x", "main"), ("me/app", "main?x=1")])
def test_snapshot_refuses_names_that_could_reach_other_api_paths(repo: str, ref: str) -> None:
    with pytest.raises(SnapshotError):
        download_snapshot(repo, ref, None)


# Comments and commits: only doc files, only from trusted comments ---------------------------


def forged_comment(files: list[str], author: str = "mallory", bot: bool = False) -> CommentRef:
    body = f"{premerge_marker(7)}\nlooks legit\n{fixes_marker(HEAD, 'e' * 40, files)}"
    return CommentRef(id=99, body=body, url="https://x/#c99", author=author, bot=bot)


def test_a_marker_comment_from_someone_without_write_access_is_ignored() -> None:
    gh = FakeCheckRepo(permission="read")
    gh.commits["e" * 40] = (HEAD, {".github/workflows/x.yml": "evil", "README.md": "evil"})
    gh.comments[99] = forged_comment(["README.md"])
    assert PreMergeChecker(gh).stored_fixes(REPO, 7, HEAD) is None


def test_stored_fixes_never_include_non_doc_paths() -> None:
    gh = FakeCheckRepo(permission="write")
    gh.commits["e" * 40] = (HEAD, {".github/workflows/x.yml": "evil", "README.md": "fixed"})
    gh.comments[99] = forged_comment([".github/workflows/x.yml", "README.md"], author="driftless[bot]", bot=True)
    assert PreMergeChecker(gh).stored_fixes(REPO, 7, HEAD) is None
    gh.comments[99] = forged_comment(["README.md"], author="driftless[bot]", bot=True)
    stored = PreMergeChecker(gh).stored_fixes(REPO, 7, HEAD)
    assert stored is not None and stored.files == {"README.md": "fixed"}


@pytest.mark.parametrize("path", [".github/workflows/ci.yml", "app/main.py", "docs/../app.py", "/README.md", "docs/x.txt"])
def test_the_github_writer_only_commits_doc_files(path: str) -> None:
    writer = GitHubWriter.__new__(GitHubWriter)  # no API: the check comes first
    with pytest.raises(ValueError, match="non-doc"):
        writer.commit_files(REPO, HEAD, {"README.md": "ok", path: "x"}, "msg")


# Web ---------------------------------------------------------------------------------------


def test_every_response_has_security_headers_and_the_inline_script_its_nonce() -> None:
    response = make().get("/")
    csp = response.headers["content-security-policy"]
    nonce = re.search(r"'nonce-([^']+)'", csp)
    assert nonce and f'<script nonce="{nonce.group(1)}">' in response.text
    assert "frame-ancestors 'none'" in csp and "unsafe-eval" not in csp
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert "max-age=" in response.headers["strict-transport-security"]
    assert 'integrity="sha384-' in response.text  # htmx is pinned by hash
    assert make().get("/health").headers["x-frame-options"] == "DENY"


def test_api_docs_are_not_served() -> None:
    client = make()
    assert all(client.get(path).status_code == 404 for path in ("/docs", "/redoc", "/openapi.json"))


def test_oversized_bodies_are_refused() -> None:
    client = make()
    big = client.post("/webhook", content=b"x" * (26 * 1024 * 1024), headers={"X-Hub-Signature-256": "sha256=0"})
    assert big.status_code == 413
    form = client.post("/r/me/app/memory/ignore", data={"value": "x" * 100_000})
    assert form.status_code == 413


def test_cross_site_fetches_are_refused_even_without_an_origin() -> None:
    sync = WebSync()
    client = make(sync)
    for site in ("cross-site", "same-site"):
        response = client.post("/r/me/app/check", data={"ref": "main"}, headers={"Sec-Fetch-Site": site})
        assert response.status_code == 403
    assert sync.check_calls == []
    ok = client.post("/r/me/app/check", data={"ref": "main"}, headers={"Sec-Fetch-Site": "same-origin"})
    assert ok.status_code == 202


def test_cross_site_admin_login_is_refused() -> None:
    client = demo()
    response = client.post("/admin/login", data={"token": ADMIN}, headers={"Origin": "https://evil.example"})
    assert response.status_code == 403


def test_failed_logins_are_capped_across_addresses() -> None:
    client = demo(client_ip_header="X-Forwarded-For")
    statuses = [
        client.post("/admin/login", data={"token": "wrong"}, headers={"X-Forwarded-For": f"10.0.{i // 250}.{i % 250}"}).status_code
        for i in range(31)
    ]
    assert statuses[:30] == [401] * 30 and statuses[30] == 429
    assert login(client).status_code == 429  # even the right token, until the window passes


def test_admin_cookie_is_httponly_secure_and_samesite() -> None:
    cookie = login(demo()).headers["set-cookie"].lower()
    assert "httponly" in cookie and "secure" in cookie and "samesite=lax" in cookie


def test_runs_on_the_public_demo_lists_allowed_repos_only() -> None:
    client = demo()
    store = client.app.state.jobs
    from driftless.jobs import JobMode

    store.enqueue("me/app", 1, JobMode.POST_MERGE)
    store.enqueue("someone/else", 2, JobMode.POST_MERGE)
    assert [job["repo"] for job in client.get("/runs").json()] == ["me/app"]


def test_the_admin_token_is_redacted_too() -> None:
    settings = Settings(_env_file=None, admin_token="admin-secret-123", database_url="sqlite://")
    assert redact("token admin-secret-123 leaked", settings) == "token [redacted] leaked"


def test_webhook_without_a_valid_signature_is_rejected() -> None:
    client = make()
    assert client.post("/webhook", content=b"{}", headers={"X-GitHub-Event": "ping"}).status_code == 401
    assert make(webhook_secret="").post(
        "/webhook", content=b"{}", headers={"X-Hub-Signature-256": "sha256=" + "0" * 64}
    ).status_code == 401
    assert WEBHOOK_SECRET  # the configured secret signs real deliveries (see test_server)
