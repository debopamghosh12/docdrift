"""Executable docs: run every example in a set of doc files against the real app, fix failures.

Per doc file: start a fresh app and run its examples in order. While one fails (and an LLM is
available), ask for a fix of the *earliest* failing example from its actual request/response,
splice it in and re-run the whole file on a fresh app (up to `max_attempts` fixes per example).
Later examples that pass after that re-run failed only because of it (a GET 404s when the POST
before it was rejected), so they are never "fixed" themselves. Each example ends up 🧪 passed,
🔁 fixed after running, ⛓️ dependent on an earlier example, or ❌ still failing.
"""

import logging
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from driftless.app_env import AppEnv, app_env
from driftless.doc_editor import proposed_docs
from driftless.doc_index import is_doc_path
from driftless.example_fixer import ExampleFixer, route_code
from driftless.examples import Example, extract_examples
from driftless.execution import ExampleOutcome, ExampleStatus, ExecutionReport
from driftless.executor import ExampleRun, run_examples
from driftless.llm import LLMError
from driftless.models import DocFile
from driftless.proposals import DocProposals
from driftless.sandbox import Sandbox, SandboxError

log = logging.getLogger(__name__)
Progress = Callable[[str], None]


class DocRunner:
    def __init__(
        self,
        sandbox: Sandbox,
        fixer: ExampleFixer | None = None,
        max_attempts: int = 2,
        max_fixes_per_file: int = 8,
        request_timeout: float = 10.0,
        progress: Progress | None = None,
    ) -> None:
        self._sandbox = sandbox
        self._fixer = fixer
        self._max_attempts = max_attempts  # fix attempts per example
        self._max_fixes_per_file = max_fixes_per_file  # LLM calls per file
        self._request_timeout = request_timeout
        self._progress = progress or (lambda message: log.info(message))

    def execute(self, docs: dict[str, str], files: dict[str, str]) -> ExecutionReport:
        """Run `docs` ({path: text}); `files` is the repo snapshot the app is started from."""
        report = ExecutionReport()
        runnable = {path: text for path, text in sorted(docs.items()) if extract_examples(path, text)}
        if not runnable:
            return report
        env = app_env(files, {**{p: t for p, t in files.items() if is_doc_path(p)}, **docs})
        try:
            with self._sandbox as box:
                report.app = box.target.spec if box.target else None
                for path, text in runnable.items():
                    self._progress(f"Running examples in {path}...")
                    try:
                        self._execute_file(box, path, text, files, env, report)
                    except SandboxError as exc:
                        report.errors.append(f"{path}: {exc}")
        except SandboxError as exc:
            report.errors.append(str(exc))
        return report

    def _execute_file(
        self, box: Sandbox, path: str, text: str, files: dict[str, str], env: AppEnv, report: ExecutionReport
    ) -> None:
        first = self._run(box, path, text, env)
        runs = first
        state = _FileState()
        while self._fixer is not None and state.fix_calls < self._max_fixes_per_file:
            target = self._next_target(runs, state)
            if target is None:
                break
            fixed_text = self._fix(text, runs, target, files, state)
            if fixed_text is None:
                continue
            rerun = self._run(box, path, fixed_text, env)
            text = fixed_text
            if len(rerun) != len(first):  # a fix changed the example count; stop rather than mislabel
                report.errors.append(f"{path}: a fix changed the number of examples; kept it unverified")
                runs = rerun
                break
            # Later examples that only start passing now were knock-on failures of this one.
            root = f"{target.example.label} (line {target.example.line})"
            for before, after in zip(runs, rerun):
                index = after.example.index
                if index > target.example.index and not before.passed and after.passed and index not in state.fixes:
                    state.caused_by[index] = root
            runs = rerun
        blocked_by = _blocked(runs, state)
        report.contents[path] = text
        for before, after in zip(first, runs):
            index = before.example.index
            report.outcomes.append(
                _outcome(
                    before, after, state.fixes.get(index, []), state.notes.get(index),
                    state.caused_by.get(index), blocked_by.get(index),
                )
            )

    def _next_target(self, runs: list[ExampleRun], state: "_FileState") -> ExampleRun | None:
        """The earliest failing example, if it may still be fixed.

        Only the earliest: a later failure is often a knock-on effect (a GET 404s because the
        POST before it never created anything), and fixing it would write that wrong behavior
        into the docs. Once the earliest can't be fixed, later failures are left alone.
        """
        failing = next((r for r in runs if not r.passed), None)
        if failing is None:
            return None
        index = failing.example.index
        if index in state.given_up or state.attempts[index] >= self._max_attempts:
            return None
        return failing

    def _run(self, box: Sandbox, path: str, text: str, env: AppEnv) -> list[ExampleRun]:
        examples = extract_examples(path, text)
        with box.serve(env.set, env.unset) as base_url:
            return run_examples(examples, base_url, timeout=self._request_timeout)

    def _fix(
        self, text: str, runs: list[ExampleRun], run: ExampleRun, files: dict[str, str], state: "_FileState"
    ) -> str | None:
        """Ask for a fix of `run`'s example: the new file text, or None (and a note) if there is none."""
        assert self._fixer is not None
        example = run.example
        state.attempts[example.index] += 1
        lines = text.split("\n")
        if _overlaps_another(example, [r.example for r in runs]):
            return state.give_up(example.index, "shares a code block with another example; not auto-fixed")
        snippet = "\n".join(lines[example.start_line : example.end_line])
        state.fix_calls += 1
        try:
            reply = self._fixer.fix(run, snippet, route_code(files, example))
        except LLMError as exc:
            return state.give_up(example.index, f"fix failed: {exc}")
        if not reply.fixed or not reply.replacement or reply.replacement.strip() == snippet.strip():
            return state.give_up(example.index, f"model could not fix it: {reply.reason}")
        state.fixes.setdefault(example.index, []).append(reply.reason)
        state.notes.pop(example.index, None)
        lines[example.start_line : example.end_line] = reply.replacement.strip("\n").split("\n")
        return "\n".join(lines)


@dataclass
class _FileState:
    """Bookkeeping of one doc file's fix loop, keyed by example index."""

    attempts: Counter[int] = field(default_factory=Counter)
    fixes: dict[int, list[str]] = field(default_factory=dict)
    notes: dict[int, str] = field(default_factory=dict)
    caused_by: dict[int, str] = field(default_factory=dict)
    given_up: set[int] = field(default_factory=set)
    fix_calls: int = 0

    def give_up(self, index: int, note: str) -> None:
        self.notes[index] = note
        self.given_up.add(index)


def _blocked(runs: list[ExampleRun], state: _FileState) -> dict[int, str]:
    """Failing examples after the earliest one that still fails: not fixed, as it may be their cause."""
    failing = [r for r in runs if not r.passed]
    if not failing:
        return {}
    root = failing[0].example
    blocked_by = f"{root.label} (line {root.line})"
    return {r.example.index: blocked_by for r in failing[1:]}


def _overlaps_another(example: Example, examples: list[Example]) -> bool:
    return any(
        other.index != example.index and other.start_line < example.end_line and example.start_line < other.end_line
        for other in examples
    )


def _outcome(
    first: ExampleRun,
    last: ExampleRun,
    fixes: list[str],
    note: str | None,
    caused_by: str | None = None,
    blocked_by: str | None = None,
) -> ExampleOutcome:
    if last.passed and first.passed:
        status = ExampleStatus.PASSED
    elif last.passed:
        status = ExampleStatus.DEPENDENT if caused_by and not fixes else ExampleStatus.FIXED
    else:
        status = ExampleStatus.FAILING
    if status == ExampleStatus.FAILING and blocked_by and not note:
        note = f"not auto-fixed: it runs after {blocked_by}, which still fails and may be the cause"
    return ExampleOutcome(
        file=last.example.file,
        label=last.example.label,
        line=last.example.line,
        status=status,
        expected_status=first.example.expected_status,
        actual_status=first.actual_status,
        final_expected_status=last.example.expected_status,
        final_actual_status=last.actual_status,
        failures=last.failures if status == ExampleStatus.FAILING else first.failures,
        warnings=last.warnings,
        fixes=fixes if status in (ExampleStatus.FIXED, ExampleStatus.FAILING) else [],
        note=note if status == ExampleStatus.FAILING else None,
        caused_by=caused_by if status == ExampleStatus.DEPENDENT else None,
        blocked_by=blocked_by if status == ExampleStatus.FAILING else None,
    )


def execute_docs(
    files: dict[str, str],
    docs: dict[str, str],
    fixer: ExampleFixer | None,
    cache_dir: Path = Path(".driftless_cache"),
    startup_timeout: float = 30.0,
    progress: Progress | None = None,
    python: str | None = None,
) -> ExecutionReport:
    sandbox = Sandbox(files, cache_dir=cache_dir, python=python, startup_timeout=startup_timeout)
    return DocRunner(sandbox, fixer, progress=progress).execute(docs, files)


def execute_all_docs(
    docs: list[DocFile],
    files: dict[str, str],
    proposals: DocProposals,
    min_confidence: float,
    fixer: ExampleFixer | None,
    cache_dir: Path = Path(".driftless_cache"),
    startup_timeout: float = 30.0,
    progress: Progress | None = None,
    python: str | None = None,
) -> ExecutionReport:
    """Run the examples in every doc file, as the proposals leave it.

    Unchanged files run too: a PR that only changes runtime behavior (a status code, a response
    field) breaks examples that static analysis has no reason to look at.
    """
    return execute_docs(
        files, proposed_docs(docs, proposals, min_confidence), fixer, cache_dir, startup_timeout, progress, python
    )
