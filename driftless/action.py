"""`driftless action`: Driftless as a GitHub Action, with nothing to host.

The workflow's event (GITHUB_EVENT_NAME + the JSON at GITHUB_EVENT_PATH) is routed exactly like
a webhook delivery (`events.route_event`) and run in the job itself:

- pull_request opened / synchronize / reopened / ready_for_review: pre-merge check (comment +
  commit status). With `fail_on_drift` the job fails too, so it can block merging.
- issue_comment `/driftless apply`: commit the fixes, then re-check the new head in the same
  job, since pushes made with the workflow's GITHUB_TOKEN don't start new workflow runs.
- issue_comment `/driftless ignore|unignore|style|unstyle|memory`: reply with the `.driftless.yml`
  change to make, since a runner's SQLite is gone when the job ends.
- pull_request closed + merged: post-merge docs PR.
- workflow_dispatch / schedule: docs health audit of the ref (`driftless check [--fix]`).

The repo's memory comes from `.driftless.yml` on its default branch (see repo_config.py).
"""

import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from driftless.commands import Command, CommentRepo, MemoryCommands
from driftless.events import EventKind, EventPlan, route_event
from driftless.memory import Memory
from driftless.pr_writer import PublishAction, PublishResult
from driftless.premerge import SupersededError
from driftless.progress import StageProgress
from driftless.repo_config import CONFIG_FILE, RepoConfig, action_memory_reply

if TYPE_CHECKING:
    from driftless.audit import AuditResult
    from driftless.config import Settings

FAILING_STATES = frozenset({"failure", "error"})


class ActionError(RuntimeError):
    """The job can't run: not inside GitHub Actions, or an unreadable event payload."""


class ActionSyncer(Protocol):
    """The runs an Action job needs (implemented by sync.GitHubSyncer)."""

    def post_merge(self, repo: str, number: int) -> PublishResult: ...

    def pre_merge(self, repo: str, number: int, head_sha: str, should_stop: Callable[[], bool]) -> PublishResult: ...

    def apply(self, repo: str, number: int, user: str) -> PublishResult: ...

    def check(self, repo: str, ref: str, fix: bool, progress: StageProgress) -> "AuditResult": ...


@dataclass
class ActionOutcome:
    exit_code: int
    summary: str
    url: str | None = None
    check_state: str | None = None  # pre-merge commit status, after apply too


def load_event(env: Mapping[str, str]) -> tuple[str, dict[str, Any]]:
    name, path = env.get("GITHUB_EVENT_NAME", ""), env.get("GITHUB_EVENT_PATH", "")
    if not name or not path:
        raise ActionError(
            "`driftless action` runs inside GitHub Actions: GITHUB_EVENT_NAME and GITHUB_EVENT_PATH must be set."
        )
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ActionError(f"Could not read the event payload at {path}: {exc}") from exc
    return name, payload


def plan_from_env(env: Mapping[str, str], fix: bool = False) -> EventPlan:
    name, payload = load_event(env)
    plan = route_event(name, payload, env.get("GITHUB_REPOSITORY", ""), env.get("GITHUB_REF_NAME", ""), fix)
    if plan.kind == EventKind.PRE_MERGE and plan.from_fork:
        # Workflows on fork PRs get no secrets and a read-only token: nothing could be posted.
        return EventPlan(kind=EventKind.IGNORED, reason="pull request from a fork (no secrets or write access in Actions)")
    return plan


class ActionMemoryCommands(MemoryCommands):
    """Memory commands in Action mode: same write-access check, but the reply is the
    `.driftless.yml` change to make instead of a change to a database that won't survive."""

    def __init__(self, github: CommentRepo, memory: Memory, config: RepoConfig) -> None:
        super().__init__(github, memory)
        self._config = config

    def respond(self, repo: str, user: str, command: Command) -> str:
        return action_memory_reply(user, command, self._config)


class ActionRunner:
    def __init__(
        self,
        syncer: ActionSyncer,
        commands: MemoryCommands,
        fail_on_drift: bool = True,
        say: Callable[[str], None] = print,
    ) -> None:
        self._syncer = syncer
        self._commands = commands
        self._fail_on_drift = fail_on_drift
        self._say = say

    def run(self, plan: EventPlan) -> ActionOutcome:
        handlers: dict[EventKind, Callable[[EventPlan], ActionOutcome]] = {
            EventKind.PRE_MERGE: lambda p: self._check(p.repo, p.number, p.head_sha),
            EventKind.POST_MERGE: self._post_merge,
            EventKind.APPLY: self._apply,
            EventKind.MEMORY: self._memory,
            EventKind.AUDIT: self._audit,
            EventKind.DOCS_PR_CLOSED: self._docs_pr_closed,
        }
        handler = handlers.get(plan.kind)
        if handler is None:
            return ActionOutcome(0, f"Nothing to do: {plan.reason}.")
        return handler(plan)

    def _check(self, repo: str, number: int, head_sha: str, gate: bool = True) -> ActionOutcome:
        self._say(f"Pre-merge check of {repo}#{number} at {head_sha[:7]}...")
        try:
            published = self._syncer.pre_merge(repo, number, head_sha, lambda: False)
        except SupersededError as exc:
            return ActionOutcome(0, f"Skipped: {exc}.")
        state = published.check_state
        if state in FAILING_STATES:
            summary = f"Docs drift found in {repo}#{number} (status: {state}): {published.url}"
        else:
            summary = f"Docs are in sync with {repo}#{number}: {published.url}"
        failed = gate and self._fail_on_drift and state in FAILING_STATES
        return ActionOutcome(1 if failed else 0, summary, published.url, state)

    def _post_merge(self, plan: EventPlan) -> ActionOutcome:
        self._say(f"Post-merge sync of {plan.repo}#{plan.number}...")
        published = self._syncer.post_merge(plan.repo, plan.number)
        if published.action == PublishAction.COMMENTED:
            return ActionOutcome(0, f"No docs PR needed; commented on #{plan.number}: {published.url}", published.url)
        return ActionOutcome(0, f"Docs PR {published.action.value}: {published.url}", published.url)

    def _apply(self, plan: EventPlan) -> ActionOutcome:
        self._say(f"/driftless apply on {plan.repo}#{plan.number} from @{plan.user}...")
        published = self._syncer.apply(plan.repo, plan.number, plan.user)
        if published.action != PublishAction.COMMITTED or not published.commit_sha:
            return ActionOutcome(0, f"Nothing committed ({published.action.value}): {published.url}", published.url)
        # The push was made with GITHUB_TOKEN, so no `synchronize` run follows: re-check here.
        recheck = self._check(plan.repo, plan.number, published.commit_sha, gate=False)
        summary = f"Committed the doc fixes: {published.url}\n{recheck.summary}"
        return ActionOutcome(recheck.exit_code, summary, published.url, recheck.check_state)

    def _memory(self, plan: EventPlan) -> ActionOutcome:
        assert plan.command is not None
        published = self._commands.run(plan.repo, plan.number, plan.user, plan.command)
        return ActionOutcome(0, f"Replied to `{plan.command.text}`: {published.url}", published.url)

    def _audit(self, plan: EventPlan) -> ActionOutcome:
        self._say(f"Docs health audit of {plan.repo}@{plan.ref}" + (" (fixing broken examples)" if plan.fix else ""))
        audit = self._syncer.check(plan.repo, plan.ref, plan.fix, lambda stage, detail: self._say(detail))
        report = audit.report
        url = audit.published.url if audit.published else None
        summary = f"{audit.summary} in {plan.repo}@{plan.ref}" + (f"; examples PR: {url}" if url else "")
        if audit.publish_note:
            summary += f" ({audit.publish_note})"
        failed = self._fail_on_drift and bool(report.failing or report.errors)
        return ActionOutcome(1 if failed else 0, summary, url, "failure" if report.failing or report.errors else "success")

    def _docs_pr_closed(self, plan: EventPlan) -> ActionOutcome:
        outcome = "merged" if plan.merged else "closed"
        return ActionOutcome(
            0,
            f"Driftless docs PR #{plan.number} was {outcome}. Action mode keeps no feedback history; "
            f"put lasting rules in {CONFIG_FILE}.",
        )


def build_runner(settings: "Settings", repo: str, fail_on_drift: bool, say: Callable[[str], None] = print) -> ActionRunner:
    """The real runner: GitHub + the LLM, with the memory `.driftless.yml` describes."""
    from driftless.github_writer import GitHubWriter
    from driftless.repo_config import config_memory, parse_repo_config
    from driftless.sync import GitHubSyncer, SyncConfigError

    if not settings.github_token:
        raise SyncConfigError("GITHUB_TOKEN is not set: pass `github-token` (defaults to the workflow's token).")
    writer = GitHubWriter(settings.github_token)
    branch, _ = writer.default_branch(repo)
    config = parse_repo_config(writer.read_file(repo, CONFIG_FILE, branch))
    if config.ignore or config.style:
        say(f"{CONFIG_FILE}: {len(config.ignore)} ignore rule(s), {len(config.style)} style preference(s)")
    memory = config_memory(repo, config)
    return ActionRunner(GitHubSyncer(settings, memory=memory), ActionMemoryCommands(writer, memory, config), fail_on_drift, say)


def write_outputs(plan: EventPlan, outcome: ActionOutcome, env: Mapping[str, str] = os.environ) -> None:
    """Step outputs (`result`, `check-state`, `url`) and a line in the job summary, when available."""
    if path := env.get("GITHUB_OUTPUT"):
        with open(path, "a", encoding="utf-8") as out:
            out.write(f"result={plan.kind.value}\ncheck-state={outcome.check_state or ''}\nurl={outcome.url or ''}\n")
    if path := env.get("GITHUB_STEP_SUMMARY"):
        with open(path, "a", encoding="utf-8") as out:
            out.write(f"### Driftless ({plan.kind.value})\n\n{outcome.summary}\n")
