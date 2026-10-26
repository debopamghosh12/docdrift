from driftless.analyzer import Analyzer
from driftless.feedback import memory_prompt, proposed_changes, remembered_line
from driftless.memory import Memory, RememberedChange, RepoMemory, matches_path
from driftless.models import DocSection
from driftless.pipeline import analyze, analyze_pull_request
from driftless.pr_writer import PublishAction, PublishResult
from driftless.premerge import PreMergeChecker
from driftless.proposals import DocProposals
from driftless.sync import executable_docs, remember_docs_pr
from tests.test_analyzer import ScriptedLLM, make_result
from tests.test_pipeline import FakeProvider
from tests.test_pr_writer import proposals, result
from tests.test_premerge import FakeCheckRepo, open_pr


def change(section: str, reason: str = "r") -> RememberedChange:
    return RememberedChange(file="README.md", section=section, kind="updated", reason=reason, diff="-old\n+new")


def test_rules_and_preferences_are_per_repo() -> None:
    memory = Memory("sqlite://")
    assert memory.add_ignore("Me/App", "CHANGELOG.md", "alice")
    assert not memory.add_ignore("me/app", "CHANGELOG.md", "bob")  # repos are case-insensitive
    memory.add_ignore("me/app", "Legacy API", "alice")
    assert memory.add_style("me/app", "use British spelling", "alice") == 1
    assert memory.add_style("me/app", "keep examples minimal", "alice") == 2
    memory.add_ignore("me/other", "README.md", "carol")

    assert memory.repo_memory("me/app") == RepoMemory(
        ignore=["CHANGELOG.md", "Legacy API"], style=["use British spelling", "keep examples minimal"]
    )
    assert memory.remove_ignore("me/app", "CHANGELOG.md") and not memory.remove_ignore("me/app", "nope")
    assert memory.remove_style("me/app", 1) == "use British spelling" and memory.remove_style("me/app", 5) is None
    assert memory.repo_memory("me/app") == RepoMemory(ignore=["Legacy API"], style=["keep examples minimal"])
    assert memory.ignore_rules("me/other") == ["README.md"]


def test_jobs_are_saved_and_unfinished_ones_interrupted() -> None:
    memory = Memory("sqlite://")
    memory.save_job("a", "me/app", 1, "succeeded", 1.0, {"id": "a", "status": "succeeded"})
    memory.save_job("b", "me/app", 2, "running", 2.0, {"id": "b", "status": "running"})
    memory.save_job("a", "me/app", 1, "succeeded", 1.0, {"id": "a", "status": "succeeded", "action": "opened"})
    assert memory.interrupt_unfinished() == 1
    assert memory.recent_jobs() == [
        {"id": "b", "status": "failed", "error": "interrupted: the server restarted before this job finished"},
        {"id": "a", "status": "succeeded", "action": "opened"},
    ]
    assert memory.recent_jobs(limit=1)[0]["id"] == "b"


def test_closed_docs_prs_feed_the_three_most_recent_rejected_changes() -> None:
    memory = Memory("sqlite://")
    memory.record_docs_pr("me/app", 10, 1, [change("A"), change("B")])
    memory.record_docs_pr("me/app", 11, 2, [change("C")])
    memory.record_docs_pr("me/app", 12, 3, [change("D"), change("E")])
    memory.record_docs_pr("me/app", 12, 3, [change("D2")])  # a rerun updated the PR: latest version wins

    assert memory.resolve_docs_pr("me/app", 10, merged=False) is not None
    assert [c.section for c in memory.resolve_docs_pr("me/app", 12, merged=False) or []] == ["D2"]
    assert memory.resolve_docs_pr("me/app", 11, merged=True) is not None
    assert memory.resolve_docs_pr("me/app", 99, merged=False) is None

    rejected = memory.recent_rejected("me/app")
    assert [(c.section, c.docs_pr) for c in rejected] == [("D2", 12), ("A", 10), ("B", 10)]
    assert memory.feedback_counts("me/app") == {"accepted": 1, "rejected": 2}
    assert memory.repo_memory("me/app").rejected == rejected


def test_rules_match_files_folders_globs_and_headings() -> None:
    assert matches_path("README.md", "README.md") and not matches_path("README.md", "docs/README.md")
    assert matches_path("docs/legacy", "docs/legacy/v1.md") and matches_path("./docs/legacy/", "docs/legacy/v1.md")
    assert matches_path("docs/*.md", "docs/api.md") and not matches_path("docs/legacy", "docs/legacy-notes.md")

    memory = RepoMemory(ignore=["docs/legacy/", "Legacy API"])
    sub = DocSection(path="README.md", heading="List", heading_path=["App", "Legacy `API`", "List"], level=3,
                     start_line=1, end_line=2, text="### List")
    assert memory.ignoring_rule(sub) == "Legacy API"  # a heading rule covers its subsections
    assert memory.ignoring_file_rule("docs/legacy/v1.md") == "docs/legacy/" and memory.ignoring_file_rule("README.md") is None


def test_ignored_sections_are_never_candidates() -> None:
    plain = analyze_pull_request(FakeProvider(), "me/lib", 7)
    assert [c.section.heading for c in plain.candidates] == ["Usage"] and plain.ignored == []

    ignored = analyze_pull_request(FakeProvider(), "me/lib", 7, memory=RepoMemory(ignore=["usage", "docs/old.md"]))
    assert ignored.candidates == []
    assert ignored.ignored == ["usage"]  # only rules that kept something out count as applied


def test_ignored_files_get_no_new_sections_and_no_example_runs() -> None:
    base = make_result()
    ignoring = RepoMemory(ignore=["docs/api.md"])
    result = analyze(base.pr, base.docs, sources=[], memory=ignoring).model_copy(update={"code": base.code})
    proposals_ = Analyzer(ScriptedLLM(), workers=1).propose(result)  # type: ignore[arg-type]
    assert {f.section.path for f in proposals_.findings} == {"README.md"}
    assert {i.proposal.file for i in proposals_.insertions} == {"README.md"}

    result.docs[1] = result.docs[1].model_copy(update={"content": "# API\n\n```bash\ncurl http://localhost:8000/notes\n```\n"})
    assert [d.path for d in executable_docs(result)] == ["README.md"]
    assert "docs/api.md" in result.ignored


def test_style_notes_and_rejected_changes_reach_the_model() -> None:
    memory = RepoMemory(style=["use British spelling"], rejected=[change("Config", "Renamed the table header")])
    result = make_result().model_copy(update={"memory": memory})
    llm = ScriptedLLM()
    proposals_ = Analyzer(llm, workers=1).propose(result)  # type: ignore[arg-type]

    assert proposals_.guided_by_memory
    batch = next(p for p in llm.prompts if '<section id="s1"' in p)
    assert "## Maintainer style preferences for this repo" in batch and "- use British spelling" in batch
    assert "## Changes the maintainers rejected before" in batch
    assert "- README.md › Config: Renamed the table header" in batch and "```diff\n-old\n+new\n```" in batch
    assert all("use British spelling" in p for p in llm.prompts)  # retries and new sections too
    assert remembered_line(result, proposals_) == (
        "🧠 Remembered for this repo: followed 1 style preference · showed the model 1 change rejected before. "
        "Reply `/driftless memory` to see everything."
    )
    assert memory_prompt(RepoMemory()) == ""


def test_remembered_line_in_the_pre_merge_comment_only_when_something_applied() -> None:
    gh = FakeCheckRepo()
    checker = PreMergeChecker(gh)
    checker.report(open_pr(), DocProposals(), 0.5)
    assert "🧠" not in gh.comments[1].body

    applied = open_pr().model_copy(update={"ignored": ["CHANGELOG.md"]})
    checker.report(applied, DocProposals(), 0.5)
    assert "🧠 Remembered for this repo: applied 1 ignore rule (`CHANGELOG.md`)." in gh.comments[1].body


def test_docs_pr_changes_are_recorded_for_feedback() -> None:
    memory = Memory("sqlite://")
    r, p = result(), proposals()
    changes = proposed_changes(r, p, 0.5, ["README.md"])
    assert [(c.section, c.kind) for c in changes] == [("Config", "updated"), ("`PATCH /a`", "added")]
    assert "+Set `NEW_VAR`." in changes[0].diff  # the dropped `Get a` rewrite was never proposed

    opened = PublishResult(action=PublishAction.OPENED, url="u", files=["README.md"], number=100)
    remember_docs_pr(memory, r, p, 0.5, opened)
    remember_docs_pr(memory, r, p, 0.5, PublishResult(action=PublishAction.COMMENTED, url="c"))
    assert [c.section for c in memory.resolve_docs_pr(r.pr.repo, 100, merged=False) or []] == ["Config", "`PATCH /a`"]
