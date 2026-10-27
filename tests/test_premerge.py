from driftless.execution import ExampleOutcome, ExampleStatus, ExecutionReport
from driftless.models import ChangedFile
from driftless.pipeline import AnalysisResult
from driftless.pr_writer import CommentRef, PRWriter, PublishAction
from driftless.premerge import STATUS_CONTEXT, PendingFixes, PreMergeChecker, PullState
from driftless.premerge_body import premerge_marker
from driftless.proposals import DocProposals
from tests.test_pr_writer import REPO, FakeGitHub, proposals, result

HEAD = "b" * 40


class FakeCheckRepo:
    """In-memory CheckRepo: comments, statuses, branches and commits."""

    def __init__(self, permission: str = "write", head_repo: str | None = REPO, open_: bool = True) -> None:
        self.comments: dict[int, CommentRef] = {}
        self.edits = 0
        self.statuses: list[tuple[str, str, str, str | None]] = []
        self.commits: dict[str, tuple[str, dict[str, str]]] = {}
        self.branches = {"feature/x": HEAD}
        self._permission = permission
        self._head_repo = head_repo
        self._open = open_

    def list_comments(self, repo: str, number: int) -> list[CommentRef]:
        return list(self.comments.values())

    def create_comment(self, repo: str, number: int, body: str) -> str:
        comment_id = len(self.comments) + 1
        url = f"https://github.com/{repo}/pull/{number}#issuecomment-{comment_id}"
        self.comments[comment_id] = CommentRef(id=comment_id, body=body, url=url, author="driftless[bot]", bot=True)
        return url

    def edit_comment(self, repo: str, number: int, comment_id: int, body: str) -> str:
        self.edits += 1
        self.comments[comment_id] = self.comments[comment_id].model_copy(update={"body": body})
        return self.comments[comment_id].url

    def set_status(self, repo: str, sha: str, state: str, description: str, target_url: str | None = None) -> None:
        self.statuses.append((sha, state, description, target_url))

    def pull_state(self, repo: str, number: int) -> PullState:
        return PullState(
            number=number, open=self._open, head_ref="feature/x", head_sha=self.branches["feature/x"],
            head_repo=self._head_repo, base_repo=REPO,
        )

    def permission(self, repo: str, user: str) -> str:
        return self._permission

    def commit_files(self, repo: str, parent_sha: str, files: dict[str, str], message: str) -> str:
        sha = f"{len(self.commits) + 1:040d}"
        self.commits[sha] = (parent_sha, dict(files))
        return sha

    def read_file(self, repo: str, path: str, ref: str) -> str | None:
        return self.commits[ref][1].get(path) if ref in self.commits else None

    def advance_branch(self, repo: str, branch: str, sha: str) -> None:
        assert self.commits[sha][0] == self.branches[branch], "not a fast-forward"
        self.branches[branch] = sha


def open_pr(head_sha: str = HEAD, **update: object) -> AnalysisResult:
    r = result()
    pr = r.pr.model_copy(update={"head_sha": head_sha, "merge_commit_sha": "", "merged": False, "head_repo": REPO, **update})
    return r.model_copy(update={"pr": pr, "ref": head_sha})


def failing_example() -> ExecutionReport:
    return ExecutionReport(
        outcomes=[
            ExampleOutcome(
                file="docs/api.md", label="GET /a", line=5, status=ExampleStatus.FAILING,
                expected_status=200, actual_status=404, failures=["status 404, docs say 200"],
            )
        ]
    )


def test_opened_pr_posts_a_comment_and_a_failing_status() -> None:
    gh = FakeCheckRepo()
    published, fixes = PreMergeChecker(gh).report(open_pr(), proposals(), 0.5)

    [comment] = gh.comments.values()
    body = comment.body
    assert body.startswith(premerge_marker(7))
    assert "## ⚠️ This PR will make 3 doc sections stale" in body
    assert "| `README.md` | Config | OLD_VAR was renamed. | updated: 🔁 fixed on retry |" in body
    assert "⚠️ dropped: route POST /a does not exist in the code" in body
    assert "<summary><code>README.md</code> (+" in body
    assert "```diff\n--- a/README.md\n+++ b/README.md" in body
    assert "+Set `NEW_VAR`." in body
    assert "Reply `/driftless apply` to commit these doc fixes to this branch." in body

    assert gh.statuses == [(HEAD, "failure", "3 stale doc sections", comment.url)]
    assert published.action == PublishAction.COMMENTED and published.check_state == "failure"
    assert fixes.head_sha == HEAD and sorted(fixes.files) == ["README.md"]


def test_in_sync_pr_gets_a_success_status() -> None:
    gh = FakeCheckRepo()
    report = ExecutionReport(
        outcomes=[ExampleOutcome(file="docs/api.md", label="GET /a", line=5, status=ExampleStatus.PASSED)]
    )
    published, fixes = PreMergeChecker(gh).report(open_pr(), DocProposals(execution=report), 0.5)

    [comment] = gh.comments.values()
    assert "## ✅ Docs are in sync with this PR" in comment.body
    assert "The 1 documented example passes against the app." in comment.body
    assert "/driftless apply" not in comment.body
    assert gh.statuses[-1][:3] == (HEAD, "success", "Docs are in sync with this PR")
    assert published.check_state == "success" and fixes.files == {}


def test_failing_examples_fail_the_status_even_without_stale_sections() -> None:
    gh = FakeCheckRepo()
    PreMergeChecker(gh).report(open_pr(), DocProposals(execution=failing_example()), 0.5)
    [comment] = gh.comments.values()
    assert "## ⚠️ This PR breaks 1 documented example (`GET /a`) in `docs/api.md`" in comment.body
    assert gh.statuses[-1][1:3] == ("failure", "1 broken example (GET /a)")


def test_review_errors_are_not_reported_as_in_sync() -> None:
    gh = FakeCheckRepo()
    PreMergeChecker(gh).report(open_pr(), DocProposals(errors=["LLM timed out"]), 0.5)
    assert gh.statuses[-1][1] == "error"


def test_synchronize_edits_the_same_comment() -> None:
    gh = FakeCheckRepo()
    checker = PreMergeChecker(gh)
    gh.create_comment(REPO, 7, "unrelated review comment")
    checker.report(open_pr(), proposals(), 0.5)
    checker.report(open_pr("d" * 40), DocProposals(), 0.5)

    assert len(gh.comments) == 2 and gh.edits == 1
    mine = gh.comments[2].body
    assert "✅ Docs are in sync" in mine and "`ddddddd`" in mine
    assert [s[:2] for s in gh.statuses] == [(HEAD, "failure"), ("d" * 40, "success")]


def test_pending_status_while_running() -> None:
    gh = FakeCheckRepo()
    PreMergeChecker(gh).start(REPO, HEAD)
    assert gh.statuses == [(HEAD, "pending", "Checking the docs against this commit...", None)]
    assert STATUS_CONTEXT == "Driftless / docs-in-sync"


def test_apply_commits_the_verified_fixes_to_the_pr_branch() -> None:
    gh = FakeCheckRepo()
    checker = PreMergeChecker(gh)
    _, fixes = checker.report(open_pr(), proposals(), 0.5)
    asked: list[str] = []

    def fixes_for(pull: PullState) -> PendingFixes:
        asked.append(pull.head_sha)
        return fixes

    published = checker.apply(REPO, 7, "maintainer", fixes_for)

    sha = gh.branches["feature/x"]
    parent, files = gh.commits[sha]
    assert parent == HEAD and asked == [HEAD]
    assert len(gh.commits) == 2  # the check's fixes commit (on no branch), then the applied one
    assert gh.branches["feature/x"] == sha
    assert "Set `NEW_VAR`." in files["README.md"] and "PATCH /a" in files["README.md"]
    assert published.action == PublishAction.COMMITTED
    assert published.url == f"https://github.com/{REPO}/commit/{sha}"
    reply = gh.comments[2].body
    assert "@maintainer" in reply and published.url in reply and "`feature/x`" in reply

    # The push re-runs the check: the docs at the new head are fixed, so it reports in sync.
    checker.report(open_pr(sha), DocProposals(), 0.5)
    assert gh.statuses[-1][:2] == (sha, "success")
    assert "✅ Docs are in sync" in gh.comments[1].body


def test_comment_points_at_a_commit_holding_exactly_its_fixes() -> None:
    gh = FakeCheckRepo()
    _, fixes = PreMergeChecker(gh).report(open_pr(), proposals(), 0.5)
    [(stash, (parent, files))] = gh.commits.items()
    assert parent == HEAD and files == fixes.files
    assert gh.branches["feature/x"] == HEAD  # on no branch
    assert stash in gh.comments[1].body

    # Another process (a fresh Actions runner) reads back what the comment showed: no LLM run.
    assert PreMergeChecker(gh).stored_fixes(REPO, 7, HEAD) == fixes


def test_stored_fixes_only_for_the_checked_head_and_an_intact_commit() -> None:
    gh = FakeCheckRepo()
    PreMergeChecker(gh).report(open_pr(), proposals(), 0.5)
    assert PreMergeChecker(gh).stored_fixes(REPO, 7, "c" * 40) is None  # the branch moved since
    gh.commits.clear()  # e.g. garbage-collected
    assert PreMergeChecker(gh).stored_fixes(REPO, 7, HEAD) is None


def test_in_sync_check_stores_nothing() -> None:
    gh = FakeCheckRepo()
    PreMergeChecker(gh).report(open_pr(), DocProposals(), 0.5)
    assert gh.commits == {}
    assert PreMergeChecker(gh).stored_fixes(REPO, 7, HEAD) is None


def test_apply_is_declined_without_write_access() -> None:
    for permission in ("read", "triage", "none"):
        gh = FakeCheckRepo(permission=permission)
        published = PreMergeChecker(gh).apply(REPO, 7, "drive-by", _never)
        assert published.action == PublishAction.DECLINED
        assert gh.commits == {} and gh.branches["feature/x"] == HEAD
        assert "only users with write access" in gh.comments[1].body


def test_apply_is_declined_for_forks_and_closed_prs() -> None:
    for gh, reason in (
        (FakeCheckRepo(head_repo="someone/app"), "in a fork"),
        (FakeCheckRepo(head_repo=None), "in a fork"),  # the fork was deleted
        (FakeCheckRepo(open_=False), "closed"),
    ):
        published = PreMergeChecker(gh).apply(REPO, 7, "maintainer", _never)
        assert published.action == PublishAction.DECLINED
        assert gh.commits == {}
        assert reason in gh.comments[1].body


def test_apply_with_nothing_to_fix_commits_nothing() -> None:
    gh = FakeCheckRepo()
    published = PreMergeChecker(gh).apply(REPO, 7, "maintainer", lambda pull: PendingFixes(head_sha=pull.head_sha))
    assert published.action == PublishAction.COMMENTED and gh.commits == {}
    assert "Nothing to apply" in gh.comments[1].body


def test_fork_pr_comment_does_not_offer_apply() -> None:
    gh = FakeCheckRepo()
    PreMergeChecker(gh).report(open_pr(head_repo="someone/app"), proposals(), 0.5)
    body = gh.comments[1].body
    assert "/driftless apply" not in body and "comes from a fork" in body


def test_merge_after_a_pre_merge_fix_comments_in_sync_without_a_docs_pr() -> None:
    merged = result()
    merged.pr.files.append(ChangedFile(path="README.md", patch="@@ -1 +1 @@\n-old\n+new"))
    github = FakeGitHub()
    published = PRWriter(github).publish(merged, DocProposals(), 0.5)
    assert published.action == PublishAction.COMMENTED and github.pulls == {}
    [comment] = github.comments.values()
    assert "already in sync" in comment.body and "updated in this PR before it was merged" in comment.body


def _never(pull: PullState) -> PendingFixes:
    raise AssertionError("fixes must not be computed for a declined apply")


def test_knock_on_example_failures_are_reported_under_their_root_cause() -> None:
    from tests.test_example_impact import notes_pr_outcomes

    gh = FakeCheckRepo()
    report = ExecutionReport(outcomes=notes_pr_outcomes())
    PreMergeChecker(gh).report(open_pr(), DocProposals(execution=report), 0.5)
    body = gh.comments[1].body

    assert "## ⚠️ This PR breaks 1 documented example (`POST /notes`) in 2 files\n" in body
    assert "4 later examples failed as a result and pass once it's fixed." in body
    assert "stale" not in body.split("\n", 2)[1]  # headline: no "6 doc sections stale"
    assert body.count("| example `POST /notes`") == 2 and "| example `GET /notes/1`" not in body
    assert "<summary>⛓️ 4 later examples failed only because of an earlier one" in body
    assert gh.statuses[-1][1:3] == ("failure", "1 broken example (POST /notes)")


def test_in_sync_comment_says_what_was_checked() -> None:
    gh = FakeCheckRepo()
    report = ExecutionReport(outcomes=[
        ExampleOutcome(file="docs/api.md", label=f"GET /{n}", line=n, status=ExampleStatus.PASSED) for n in range(3)
    ])
    r = open_pr()
    PreMergeChecker(gh).report(r.model_copy(update={"candidates": []}), DocProposals(execution=report), 0.5)
    body = gh.comments[1].body
    assert "checked 0" not in body
    sections, files = len(r.sections), len(r.docs)
    assert (
        f"Driftless checked {sections} doc sections and ran 3 examples across {files} doc file"
        in body
    )
    assert "All 3 documented examples pass against the app." in body
