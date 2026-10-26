"""`driftless action`: event routing from sample payloads, and what each route runs."""

from collections.abc import Callable
from pathlib import Path

import pytest
from typer.testing import CliRunner

from driftless.action import ActionError, ActionMemoryCommands, ActionRunner, load_event, plan_from_env, write_outputs
from driftless.audit import AuditResult
from driftless.cli import app
from driftless.events import EventKind
from driftless.execution import ExecutionReport
from driftless.pr_writer import PublishAction, PublishResult
from driftless.premerge import SupersededError
from driftless.progress import StageProgress
from driftless.repo_config import RepoConfig, config_memory

EVENTS = Path(__file__).parent / "fixtures" / "events"
REPO = "acme/notes"


def env_for(event: str, fixture: str, **extra: str) -> dict[str, str]:
    return {"GITHUB_EVENT_NAME": event, "GITHUB_EVENT_PATH": str(EVENTS / f"{fixture}.json"), **extra}


# Routing ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("event", "fixture", "kind"),
    [
        ("pull_request", "pull_request_opened", EventKind.PRE_MERGE),
        ("pull_request", "pull_request_synchronize", EventKind.PRE_MERGE),
        ("pull_request", "pull_request_merged", EventKind.POST_MERGE),
        ("pull_request", "docs_pr_merged", EventKind.DOCS_PR_CLOSED),
        ("issue_comment", "issue_comment_apply", EventKind.APPLY),
        ("issue_comment", "issue_comment_ignore", EventKind.MEMORY),
        ("workflow_dispatch", "workflow_dispatch", EventKind.AUDIT),
        ("schedule", "schedule", EventKind.AUDIT),
    ],
)
def test_events_route_to_their_job(event: str, fixture: str, kind: EventKind) -> None:
    assert plan_from_env(env_for(event, fixture, GITHUB_REPOSITORY=REPO, GITHUB_REF_NAME="main")).kind == kind


@pytest.mark.parametrize(
    ("event", "fixture", "reason"),
    [
        ("pull_request", "pull_request_draft", "draft pull request"),
        ("pull_request", "pull_request_fork", "pull request from a fork"),
        ("pull_request", "pull_request_labeled", "pull_request action 'labeled'"),
        ("pull_request", "pull_request_closed_unmerged", "closed without merging"),
        ("issue_comment", "issue_comment_chatter", "not a Driftless command"),
        ("issue_comment", "issue_comment_on_issue", "not on a pull request"),
        ("issue_comment", "issue_comment_apply_closed_pr", "pull request is closed"),
        ("push", "pull_request_opened", "event 'push'"),
    ],
)
def test_other_events_are_ignored_with_a_reason(event: str, fixture: str, reason: str) -> None:
    plan = plan_from_env(env_for(event, fixture))
    assert plan.kind == EventKind.IGNORED
    assert reason in plan.reason


def test_pre_merge_plan_carries_the_pushed_head() -> None:
    plan = plan_from_env(env_for("pull_request", "pull_request_synchronize"))
    assert (plan.repo, plan.number, plan.head_sha) == (REPO, 7, "fff0000aaa1111")


def test_comment_plans_carry_the_commenter_and_command() -> None:
    plan = plan_from_env(env_for("issue_comment", "issue_comment_ignore"))
    assert plan.user == "maintainer"
    assert plan.command is not None and plan.command.argument == "CHANGELOG.md"


def test_dispatch_audits_the_dispatched_ref_and_honours_the_fix_input() -> None:
    plan = plan_from_env(env_for("workflow_dispatch", "workflow_dispatch"))
    assert (plan.repo, plan.ref, plan.fix) == (REPO, "release", True)


def test_schedule_payload_falls_back_to_the_runner_env() -> None:
    plan = plan_from_env(env_for("schedule", "schedule", GITHUB_REPOSITORY=REPO, GITHUB_REF_NAME="main"), fix=False)
    assert (plan.repo, plan.ref, plan.fix) == (REPO, "main", False)


def test_missing_event_env_explains_itself(tmp_path: Path) -> None:
    with pytest.raises(ActionError, match="inside GitHub Actions"):
        load_event({})
    with pytest.raises(ActionError, match="Could not read"):
        load_event({"GITHUB_EVENT_NAME": "push", "GITHUB_EVENT_PATH": str(tmp_path / "missing.json")})


# Running ------------------------------------------------------------------------------------


class FakeSyncer:
    def __init__(self, check_states: list[str] | None = None, apply_action: PublishAction = PublishAction.COMMITTED) -> None:
        self.calls: list[tuple] = []
        self._states = list(check_states or ["success"])
        self._apply_action = apply_action
        self.audit_report = ExecutionReport()
        self.superseded = False

    def post_merge(self, repo: str, number: int) -> PublishResult:
        self.calls.append(("post_merge", repo, number))
        return PublishResult(action=PublishAction.OPENED, url="https://github.com/acme/notes/pull/12", number=12)

    def pre_merge(self, repo: str, number: int, head_sha: str, should_stop: Callable[[], bool]) -> PublishResult:
        self.calls.append(("pre_merge", repo, number, head_sha))
        if self.superseded:
            raise SupersededError("acme/notes#7 moved")
        state = self._states.pop(0)
        return PublishResult(action=PublishAction.COMMENTED, url="https://github.com/c/1", check_state=state)

    def apply(self, repo: str, number: int, user: str) -> PublishResult:
        self.calls.append(("apply", repo, number, user))
        if self._apply_action != PublishAction.COMMITTED:
            return PublishResult(action=self._apply_action, url="https://github.com/c/2")
        return PublishResult(
            action=PublishAction.COMMITTED, url="https://github.com/acme/notes/commit/new5ha", commit_sha="new5ha"
        )

    def check(self, repo: str, ref: str, fix: bool, progress: StageProgress) -> AuditResult:
        self.calls.append(("check", repo, ref, fix))
        return AuditResult(repo=repo, ref=ref, report=self.audit_report, originals={}, fix=fix)


class FakeComments:
    def __init__(self, permission: str = "write") -> None:
        self._permission = permission
        self.comments: list[str] = []

    def permission(self, repo: str, user: str) -> str:
        return self._permission

    def create_comment(self, repo: str, number: int, body: str) -> str:
        self.comments.append(body)
        return f"https://github.com/{repo}/pull/{number}#issuecomment-{len(self.comments)}"


def make_runner(
    syncer: FakeSyncer, comments: FakeComments | None = None, config: RepoConfig | None = None, fail_on_drift: bool = True
) -> ActionRunner:
    config = config or RepoConfig()
    commands = ActionMemoryCommands(comments or FakeComments(), config_memory(REPO, config), config)
    return ActionRunner(syncer, commands, fail_on_drift, say=lambda text: None)


def plan(event: str, fixture: str):
    return plan_from_env(env_for(event, fixture, GITHUB_REPOSITORY=REPO, GITHUB_REF_NAME="main"))


@pytest.mark.parametrize(("state", "fail_on_drift", "exit_code"), [
    ("success", True, 0), ("failure", True, 1), ("error", True, 1), ("failure", False, 0),
])
def test_pre_merge_check_fails_the_job_on_drift_when_asked(state: str, fail_on_drift: bool, exit_code: int) -> None:
    syncer = FakeSyncer([state])
    outcome = make_runner(syncer, fail_on_drift=fail_on_drift).run(plan("pull_request", "pull_request_opened"))
    assert syncer.calls == [("pre_merge", REPO, 7, "abc1234def5678")]
    assert (outcome.exit_code, outcome.check_state) == (exit_code, state)


def test_superseded_check_is_not_a_failure() -> None:
    syncer = FakeSyncer()
    syncer.superseded = True
    outcome = make_runner(syncer).run(plan("pull_request", "pull_request_opened"))
    assert outcome.exit_code == 0 and "Skipped" in outcome.summary


def test_merged_pr_opens_the_docs_pr() -> None:
    syncer = FakeSyncer()
    outcome = make_runner(syncer).run(plan("pull_request", "pull_request_merged"))
    assert syncer.calls == [("post_merge", REPO, 7)]
    assert outcome.exit_code == 0 and outcome.url == "https://github.com/acme/notes/pull/12"


def test_apply_rechecks_the_pushed_commit_in_the_same_job() -> None:
    # GITHUB_TOKEN pushes start no `synchronize` run, so the job checks the new head itself.
    syncer = FakeSyncer(["success"])
    outcome = make_runner(syncer).run(plan("issue_comment", "issue_comment_apply"))
    assert syncer.calls == [("apply", REPO, 7, "maintainer"), ("pre_merge", REPO, 7, "new5ha")]
    assert outcome.check_state == "success"
    assert outcome.url == "https://github.com/acme/notes/commit/new5ha"


def test_apply_recheck_reports_drift_without_failing_the_comment_run() -> None:
    syncer = FakeSyncer(["failure"])
    outcome = make_runner(syncer).run(plan("issue_comment", "issue_comment_apply"))
    assert (outcome.exit_code, outcome.check_state) == (0, "failure")


def test_declined_apply_does_not_recheck() -> None:
    syncer = FakeSyncer(apply_action=PublishAction.DECLINED)
    outcome = make_runner(syncer).run(plan("issue_comment", "issue_comment_apply"))
    assert syncer.calls == [("apply", REPO, 7, "maintainer")]
    assert outcome.exit_code == 0


def test_memory_command_replies_with_the_driftless_yml_lines() -> None:
    comments = FakeComments()
    config = RepoConfig(ignore=["docs/legacy/"], style=["Use British spelling."])
    outcome = make_runner(FakeSyncer(), comments, config).run(plan("issue_comment", "issue_comment_ignore"))
    assert outcome.exit_code == 0
    (reply,) = comments.comments
    assert ".driftless.yml" in reply and "GitHub Action mode" in reply
    assert "  - docs/legacy/\n  - CHANGELOG.md   # new" in reply
    assert "style:\n  - Use British spelling." in reply


def test_memory_command_needs_write_access_in_action_mode_too() -> None:
    comments = FakeComments(permission="read")
    make_runner(FakeSyncer(), comments).run(plan("issue_comment", "issue_comment_ignore"))
    assert "only users with write access" in comments.comments[0]


@pytest.mark.parametrize(("errors", "fail_on_drift", "exit_code"), [([], True, 0), (["boom"], True, 1), (["boom"], False, 0)])
def test_audit_runs_the_docs_health_check(errors: list[str], fail_on_drift: bool, exit_code: int) -> None:
    syncer = FakeSyncer()
    syncer.audit_report = ExecutionReport(errors=errors)
    outcome = make_runner(syncer, fail_on_drift=fail_on_drift).run(plan("workflow_dispatch", "workflow_dispatch"))
    assert syncer.calls == [("check", REPO, "release", True)]
    assert outcome.exit_code == exit_code


def test_closed_docs_pr_is_acknowledged_without_feedback_storage() -> None:
    syncer = FakeSyncer()
    outcome = make_runner(syncer).run(plan("pull_request", "docs_pr_merged"))
    assert syncer.calls == [] and outcome.exit_code == 0 and ".driftless.yml" in outcome.summary


def test_outputs_and_job_summary_are_written(tmp_path: Path) -> None:
    syncer = FakeSyncer(["failure"])
    the_plan = plan("pull_request", "pull_request_opened")
    outcome = make_runner(syncer).run(the_plan)
    out, summary = tmp_path / "out", tmp_path / "summary.md"
    write_outputs(the_plan, outcome, {"GITHUB_OUTPUT": str(out), "GITHUB_STEP_SUMMARY": str(summary)})
    assert out.read_text(encoding="utf-8") == "result=pre-merge\ncheck-state=failure\nurl=https://github.com/c/1\n"
    assert "Docs drift found" in summary.read_text(encoding="utf-8")


def test_cli_does_nothing_for_ignored_events(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in env_for("pull_request", "pull_request_draft").items():
        monkeypatch.setenv(key, value)
    result = CliRunner().invoke(app, ["action"])
    assert result.exit_code == 0
    assert "Nothing to do: draft pull request" in result.output


def test_cli_outside_actions_fails_with_an_annotation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)
    monkeypatch.delenv("GITHUB_EVENT_PATH", raising=False)
    result = CliRunner().invoke(app, ["action"])
    assert result.exit_code == 1
    assert "::error title=Driftless::" in result.output
