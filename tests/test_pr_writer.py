import pytest

from driftless.doc_index import index_document
from driftless.execution import ExampleOutcome, ExampleStatus, ExecutionReport
from driftless.models import DocFile, MissingDoc, PullRequestInfo
from driftless.pipeline import AnalysisResult, OwnPullRequestError, analyze
from driftless.pr_writer import CommentRef, PRWriter, PublishAction, PullRequestRef
from driftless.proposals import (
    CoveredItem,
    DocProposals,
    Insertion,
    NewSectionProposal,
    SectionVerdict,
    StaleFinding,
    Verification,
    VerificationStatus,
)

REPO = "me/app"
README = DocFile(
    path="README.md",
    content="# App\n\n## Config\n\nSet `OLD_VAR`.\n\n## Endpoints\n\n### `GET /a`\n\nGet a.\n",
)
API = DocFile(path="docs/api.md", content="# API\n\n## Get a\n\n`GET /a` returns a.\n")


class FakeGitHub:
    """In-memory DocsRepo recording every write."""

    def __init__(self) -> None:
        self.files = {"README.md": README.content, "docs/api.md": API.content}
        self.commits: dict[str, tuple[str, str, dict[str, str]]] = {"base": ("", "", {})}
        self.branches: dict[str, str] = {"main": "base"}
        self.pulls: dict[int, dict[str, str]] = {}
        self.comments: dict[int, CommentRef] = {}

    def default_branch(self, repo: str) -> tuple[str, str]:
        return "main", self.branches["main"]

    def read_file(self, repo: str, path: str, ref: str) -> str | None:
        return self.files.get(path)

    def commit_files(self, repo: str, parent_sha: str, files: dict[str, str], message: str) -> str:
        sha = f"c{len(self.commits)}"
        self.commits[sha] = (parent_sha, message, dict(files))
        return sha

    def set_branch(self, repo: str, branch: str, sha: str) -> None:
        self.branches[branch] = sha

    def find_open_pull(self, repo: str, head_branch: str) -> PullRequestRef | None:
        for number, pull in self.pulls.items():
            if pull["head"] == head_branch:
                return PullRequestRef(number=number, url=f"https://github.com/{repo}/pull/{number}")
        return None

    def create_pull(self, repo: str, head: str, base: str, title: str, body: str) -> PullRequestRef:
        number = 100 + len(self.pulls)
        self.pulls[number] = {"head": head, "base": base, "title": title, "body": body}
        return PullRequestRef(number=number, url=f"https://github.com/{repo}/pull/{number}")

    def update_pull(self, repo: str, number: int, title: str, body: str) -> PullRequestRef:
        self.pulls[number].update(title=title, body=body)
        return PullRequestRef(number=number, url=f"https://github.com/{repo}/pull/{number}")

    def list_comments(self, repo: str, number: int) -> list[CommentRef]:
        return list(self.comments.values())

    def create_comment(self, repo: str, number: int, body: str) -> str:
        comment_id = len(self.comments) + 1
        url = f"https://github.com/{repo}/pull/{number}#issuecomment-{comment_id}"
        self.comments[comment_id] = CommentRef(id=comment_id, body=body, url=url)
        return url

    def edit_comment(self, repo: str, comment_id: int, body: str) -> str:
        self.comments[comment_id] = self.comments[comment_id].model_copy(update={"body": body})
        return self.comments[comment_id].url


def result(head_ref: str = "feature/x") -> AnalysisResult:
    pr = PullRequestInfo(
        repo=REPO,
        number=7,
        title="Rename OLD_VAR",
        url=f"https://github.com/{REPO}/pull/7",
        base_ref="main",
        head_ref=head_ref,
        merge_base_sha="a" * 40,
        head_sha="b" * 40,
        merge_commit_sha="c" * 40,
    )
    return analyze(pr, [README, API])


def status(kind: VerificationStatus, failures: list[str] | None = None) -> Verification:
    return Verification(status=kind, failures=failures or [])


def proposals() -> DocProposals:
    config = next(s for s in index_document(README) if s.heading == "Config")
    get_a = next(s for s in index_document(API) if s.heading == "Get a")
    return DocProposals(
        findings=[
            StaleFinding(
                section=config,
                matched_terms=["OLD_VAR"],
                verdict=SectionVerdict(
                    stale=True, confidence=0.9, reason="OLD_VAR was renamed. It is now NEW_VAR.",
                    updated_section="## Config\n\nSet `NEW_VAR`.",
                ),
                verification=status(VerificationStatus.FIXED_ON_RETRY, ["`NEWVAR` is not an env var"]),
            ),
            StaleFinding(
                section=get_a,
                matched_terms=["GET /a"],
                verdict=SectionVerdict(
                    stale=True, confidence=0.8, reason="Pipe | in reason.", updated_section="## Get a\n\nWrong."
                ),
                verification=Verification(
                    status=VerificationStatus.DROPPED,
                    failures=["route PUT /a does not exist in the code"],
                    retry_failures=["route POST /a does not exist in the code"],
                ),
            ),
        ],
        insertions=[
            Insertion(
                item=MissingDoc(kind="route", name="PATCH /a", file="main.py"),
                proposal=NewSectionProposal(
                    file="README.md", after_heading="GET /a", section="### `PATCH /a`\n\nPatch a.",
                    reason="New endpoint.", confidence=0.85,
                ),
                verification=status(VerificationStatus.VERIFIED),
            )
        ],
        covered=[CoveredItem(item=MissingDoc(kind="env_var", name="NEW_VAR", file="cfg.py"), file="README.md")],
    )


def test_opens_one_commit_pr_with_verified_changes_only() -> None:
    gh = FakeGitHub()
    published = PRWriter(gh).publish(result(), proposals(), 0.5)

    assert published.action == PublishAction.OPENED
    assert published.url == f"https://github.com/{REPO}/pull/100"
    assert published.branch == "driftless/pr-7" and published.files == ["README.md"]
    parent, message, files = gh.commits[gh.branches["driftless/pr-7"]]
    assert (parent, message) == ("base", "docs: sync with #7")
    assert files == {"README.md": "# App\n\n## Config\n\nSet `NEW_VAR`.\n\n## Endpoints\n\n### `GET /a`\n\nGet a.\n\n"
                     "### `PATCH /a`\n\nPatch a.\n"}  # docs/api.md change was dropped

    pull = gh.pulls[100]
    assert pull["title"] == "docs: sync docs with #7 (Rename OLD_VAR)"
    assert (pull["head"], pull["base"]) == ("driftless/pr-7", "main")
    body = pull["body"]
    assert f"[{REPO}#7](https://github.com/{REPO}/pull/7)" in body
    assert "Updated 1 section, added 1 section across 1 file." in body
    assert "| `README.md` | Config | updated | OLD_VAR was renamed. | 0.90 | 🔁 fixed on retry |" in body
    assert "| `README.md` | `PATCH /a` (after GET /a) | added | New endpoint. | 0.85 | ✅ verified |" in body
    assert "Pipe \\| in reason." in body  # table cells are escaped
    assert "⚠️ dropped: route POST /a does not exist in the code" in body
    assert "### Dropped changes\n\n- `docs/api.md` › Get a: route POST /a does not exist" in body
    assert "<details>" in body and "> OLD_VAR was renamed. It is now NEW_VAR." in body
    assert "- First draft failed verification: `NEWVAR` is not an env var" in body
    assert "| `README.md` | NEW_VAR | covered |" in body
    assert body.splitlines()[1] == "🤖 Generated by Driftless"  # banner right after the hidden marker
    assert body.rstrip().endswith("Generated by Driftless</sub>")


def test_rerun_updates_branch_and_pr_instead_of_failing() -> None:
    gh = FakeGitHub()
    writer = PRWriter(gh)
    first = writer.publish(result(), proposals(), 0.5)
    first_commit = gh.branches["driftless/pr-7"]
    second = writer.publish(result(), proposals(), 0.5)

    assert second.action == PublishAction.UPDATED and second.url == first.url
    assert len(gh.pulls) == 1
    assert gh.branches["driftless/pr-7"] != first_commit
    assert gh.commits[gh.branches["driftless/pr-7"]][0] == "base"  # rebuilt on the default branch


def test_in_sync_comments_on_source_pr_and_edits_on_rerun() -> None:
    gh = FakeGitHub()
    writer = PRWriter(gh)
    published = writer.publish(result(), DocProposals(), 0.5)
    assert published.action == PublishAction.COMMENTED
    assert published.url.endswith("#issuecomment-1")
    (comment,) = gh.comments.values()
    assert "the docs are already in sync" in comment.body and "<!-- driftless:source-pr=7 -->" in comment.body
    assert not gh.pulls and set(gh.branches) == {"main"}

    writer.publish(result(), DocProposals(), 0.5)
    assert len(gh.comments) == 1


def test_rerun_edits_comment_posted_before_the_rename() -> None:
    gh = FakeGitHub()
    gh.comments[1] = CommentRef(id=1, body="<!-- docdrift:source-pr=7 -->\nold", url="https://x/#issuecomment-1")
    PRWriter(gh).publish(result(), DocProposals(), 0.5)
    (comment,) = gh.comments.values()
    assert "<!-- driftless:source-pr=7 -->" in comment.body


def test_all_changes_dropped_comments_with_reasons() -> None:
    gh = FakeGitHub()
    only_dropped = proposals()
    only_dropped.findings = only_dropped.findings[1:]
    only_dropped.insertions = []
    published = PRWriter(gh).publish(result(), only_dropped, 0.5)
    assert published.action == PublishAction.COMMENTED
    (comment,) = gh.comments.values()
    assert "none of its proposed changes passed verification" in comment.body
    assert "route POST /a does not exist in the code" in comment.body


def test_files_changed_on_default_branch_are_not_overwritten() -> None:
    gh = FakeGitHub()
    gh.files["README.md"] = README.content + "\nSomeone else's edit.\n"
    published = PRWriter(gh).publish(result(), proposals(), 0.5)
    assert published.action == PublishAction.COMMENTED
    assert published.skipped_files == ["README.md"]
    assert "changed on the default branch" in next(iter(gh.comments.values())).body


@pytest.mark.parametrize("head_ref", ["driftless/pr-3", "docdrift/pr-3"])
def test_never_runs_on_driftless_branches(head_ref: str) -> None:
    gh = FakeGitHub()
    with pytest.raises(OwnPullRequestError):
        PRWriter(gh).publish(result(head_ref=head_ref), proposals(), 0.5)
    assert not gh.pulls and not gh.comments and set(gh.branches) == {"main"}


def example_report(readme: str) -> ExecutionReport:
    fixed = ExampleOutcome(
        file="README.md", label="GET /a", line=9, status=ExampleStatus.FIXED, expected_status=201,
        actual_status=200, final_expected_status=200, final_actual_status=200,
        failures=["status is 200, docs say 201"], fixes=["GET /a returns 200."],
    )
    return ExecutionReport(outcomes=[fixed], contents={"README.md": readme, "docs/api.md": API.content}, app="app:app")


def test_check_fix_opens_one_examples_pr_and_updates_it_on_rerun() -> None:
    gh = FakeGitHub()
    originals = {"README.md": README.content, "docs/api.md": API.content}
    fixed_readme = README.content.replace("Get a.", "Get a. `200 OK`")

    published = PRWriter(gh).publish_example_fixes(REPO, "main", originals, example_report(fixed_readme))
    assert published is not None and published.action == PublishAction.OPENED
    assert published.branch == "driftless/fix-examples" and published.files == ["README.md"]
    pull = gh.pulls[100]
    assert pull["title"] == "docs: fix examples that fail when run"
    assert "### 🧪 Executed examples: 1/1 passing" in pull["body"]
    assert "🔁 fixed after running" in pull["body"]

    again = PRWriter(gh).publish_example_fixes(REPO, "main", originals, example_report(fixed_readme))
    assert again is not None and again.action == PublishAction.UPDATED and len(gh.pulls) == 1

    assert PRWriter(gh).publish_example_fixes(REPO, "main", originals, example_report(README.content)) is None
