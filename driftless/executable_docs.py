"""Executable docs: run every example in a set of doc files against the real app, fix failures.

Per doc file: start a fresh app and run its examples in order. While one fails (and an LLM is
available), ask for a fix of the *earliest* failing example from its actual request/response,
splice it in and re-run the whole file on a fresh app (up to `max_attempts` fixes per example).
A value the fix changed in the request is also updated in later documented responses that echo it
(echo.py), confirmed by another run. A fix is kept only if the example then passes, and happy paths stay happy: a fix may never turn a
documented 2xx into an error. When no request succeeds, the example "needs a human" and its docs
are left alone. Later examples that pass after that re-run failed only because of it, and a 404 on
a resource whose create (POST/PUT) failed is a knock-on: neither is ever "fixed" itself. Each
example ends up 🧪 passed, 🔁 fixed after running, ⛓️ dependent on an earlier example, or ❌ failing.
"""

import logging
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field

from driftless.app_env import AppEnv, app_env
from driftless.doc_editor import proposed_docs
from driftless.doc_index import is_doc_path
from driftless.echo import changed_request_values, echo_request_values
from driftless.example_fixer import ExampleFixer, route_code
from driftless.examples import Example, extract_examples
from driftless.execution import ExampleOutcome, ExampleStatus, ExecutionReport, is_success
from driftless.executor import ExampleRun, run_examples
from driftless.llm import LLMError
from driftless.models import DocFile
from driftless.proposals import DocProposals
from driftless.sandbox import Sandbox, SandboxError, SandboxLimits

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
            fix = self._fix(path, text, runs, target, files, state)
            if fix is None:
                continue
            fixed_text, reason = fix
            rerun = self._run(box, path, fixed_text, env)
            index = target.example.index
            if len(rerun) != len(first):  # a fix changed the example count; stop rather than mislabel
                report.errors.append(f"{path}: a fix changed the number of examples; kept it unverified")
                state.fixes.setdefault(index, []).append(reason)
                text, runs = fixed_text, rerun
                break
            if not rerun[index].passed:
                # Only fixes that make the example pass when run reach the docs.
                state.reject(index, f"it still failed when run: {'; '.join(rerun[index].failures)}")
                continue
            fixed_text, rerun, echoed = self._echo(box, path, env, target.example, fixed_text, rerun)
            if echoed:
                stop = "" if reason.rstrip().endswith((".", "!", "?")) else "."
                reason = f"{reason.rstrip()}{stop} Also updated it where documented responses echo it: {'; '.join(echoed)}."
            state.fixes.setdefault(index, []).append(reason)
            state.notes.pop(index, None)
            # Later examples that only start passing now were knock-on failures of this one.
            root = _ref(target.example)
            for before, after in zip(runs, rerun):
                later = after.example.index
                if later > index and not before.passed and after.passed and later not in state.fixes:
                    state.caused_by[later] = root
            text, runs = fixed_text, rerun
        blocked = _blocked(runs)
        report.contents[path] = text
        for before, after in zip(first, runs):
            index = before.example.index
            report.outcomes.append(
                _outcome(
                    before, after, state.fixes.get(index, []), state.notes.get(index),
                    state.caused_by.get(index), blocked.get(index),
                    None if index in blocked else _needs_human(before, after, state),
                )
            )

    def _next_target(self, runs: list[ExampleRun], state: "_FileState") -> ExampleRun | None:
        """The earliest failing example, if it may still be fixed.

        Only the earliest: a later failure is often a knock-on effect (a GET 404s because the
        POST before it never created anything), and fixing it would write that wrong behavior
        into the docs. Once the earliest can't be fixed, later failures are left alone. A 404 on
        a resource whose create failed is skipped outright: it is never the docs' fault.
        """
        failing = next((r for r in runs if not r.passed and _failed_create(r, runs) is None), None)
        if failing is None:
            return None
        index = failing.example.index
        if index in state.given_up or state.attempts[index] >= self._max_attempts:
            return None
        return failing

    def _echo(
        self, box: Sandbox, path: str, env: AppEnv, before: Example, text: str, runs: list[ExampleRun]
    ) -> tuple[str, list[ExampleRun], list[str]]:
        """Update later documented responses that echo a value the fix changed in the request
        (see echo.py), then re-run the file: kept only if no example that passed now fails."""
        changes = changed_request_values(before, runs[before.index].example)
        echoed, notes = echo_request_values(text, before.index, changes, runs) if changes else (text, [])
        if not notes:
            return text, runs, []
        confirm = self._run(box, path, echoed, env)
        if len(confirm) != len(runs) or any(r.passed and not c.passed for r, c in zip(runs, confirm)):
            return text, runs, []
        return echoed, confirm, notes

    def _run(self, box: Sandbox, path: str, text: str, env: AppEnv) -> list[ExampleRun]:
        examples = extract_examples(path, text)
        with box.serve(env.set, env.unset) as base_url:
            return run_examples(examples, base_url, timeout=self._request_timeout)

    def _fix(
        self, path: str, text: str, runs: list[ExampleRun], run: ExampleRun, files: dict[str, str], state: "_FileState"
    ) -> tuple[str, str] | None:
        """Ask for a fix of `run`'s example: (new file text, reason), or None (and a note) if there is none."""
        assert self._fixer is not None
        example = run.example
        state.attempts[example.index] += 1
        lines = text.split("\n")
        if _overlaps_another(example, [r.example for r in runs]):
            return state.give_up(example.index, "shares a code block with another example; not auto-fixed")
        snippet = "\n".join(lines[example.start_line : example.end_line])
        state.fix_calls += 1
        try:
            reply = self._fixer.fix(run, snippet, route_code(files, example), state.rejected.get(example.index))
        except LLMError as exc:
            return state.give_up(example.index, f"fix failed: {exc}")
        if not reply.fixed or not reply.replacement or reply.replacement.strip() == snippet.strip():
            state.reasons[example.index] = reply.reason
            return state.give_up(example.index, f"model could not fix it: {reply.reason}")
        new_status = _documented_status(path, reply.replacement)
        if is_success(example.expected_status) and not is_success(new_status):
            # Documenting the error a happy-path request now gets is not a fix.
            return state.reject(
                example.index, f"it documented {new_status} instead of keeping the {_success(example)} success"
            )
        lines[example.start_line : example.end_line] = reply.replacement.strip("\n").split("\n")
        return "\n".join(lines), reply.reason


@dataclass
class _FileState:
    """Bookkeeping of one doc file's fix loop, keyed by example index."""

    attempts: Counter[int] = field(default_factory=Counter)
    fixes: dict[int, list[str]] = field(default_factory=dict)
    notes: dict[int, str] = field(default_factory=dict)
    caused_by: dict[int, str] = field(default_factory=dict)
    given_up: set[int] = field(default_factory=set)
    rejected: dict[int, list[str]] = field(default_factory=dict)  # why each thrown-away fix was rejected
    reasons: dict[int, str] = field(default_factory=dict)  # the model's reason when it could not fix one
    fix_calls: int = 0

    def give_up(self, index: int, note: str) -> None:
        self.notes[index] = note
        self.given_up.add(index)

    def reject(self, index: int, why: str) -> None:
        self.rejected.setdefault(index, []).append(why)
        self.notes[index] = f"fix rejected: {why}"


_CREATES = frozenset({"POST", "PUT"})
_READS = frozenset({"GET", "HEAD", "PUT", "PATCH", "DELETE"})


def _failed_create(run: ExampleRun, runs: list[ExampleRun]) -> Example | None:
    """The earlier POST/PUT that should have created the resource `run` 404s on, if it failed."""
    example = run.example
    if run.actual_status != 404 or example.method not in _READS:
        return None
    path = _resource(example.path)
    for earlier in reversed(runs[: example.index]):
        create = earlier.example
        base = _resource(create.path)
        if create.method in _CREATES and (path == base or path.startswith(base + "/")) and not _succeeded(earlier):
            return create
    return None


def _resource(path: str) -> str:
    return path.split("?", 1)[0].rstrip("/") or "/"


def _succeeded(run: ExampleRun) -> bool:
    return run.actual_status is not None and is_success(run.actual_status)


def _blocked(runs: list[ExampleRun]) -> dict[int, tuple[str, bool]]:
    """Failing examples not to fix: what each waits on, and whether that is certain.

    Certain: a 404 after the create it reads failed. Possible: any other failure after the
    earliest one that still fails, as that one may be the cause.
    """
    blocked: dict[int, tuple[str, bool]] = {}
    root: Example | None = None
    for run in runs:
        if run.passed:
            continue
        create = _failed_create(run, runs)
        if create is not None:
            blocked[run.example.index] = (_ref(create), True)
        elif root is None:
            root = run.example
        else:
            blocked[run.example.index] = (_ref(root), False)
    return blocked


def _needs_human(first: ExampleRun, last: ExampleRun, state: _FileState) -> str | None:
    """Why a happy-path example whose request is now rejected could not be fixed, or None."""
    index = first.example.index
    if last.passed or not state.attempts[index] or not is_success(first.example.expected_status):
        return None
    if _succeeded(last):
        return None  # the request works; only the documented result is off
    if index in state.reasons:
        return state.reasons[index]
    if index in state.rejected:
        return (
            f"no fix keeps the documented {_success(first.example)} success "
            f"(the request gets {_status_text(last.actual_status)})"
        )
    return state.notes.get(index)


def _documented_status(path: str, replacement: str) -> int | None:
    found = extract_examples(path, replacement)
    return found[0].expected_status if found else None


def _success(example: Example) -> str:
    return str(example.expected_status) if example.expected_status is not None else "2xx"


def _status_text(status: int | None) -> str:
    return str(status) if status is not None else "no response"


def _ref(example: Example) -> str:
    return f"{example.label} (line {example.line})"


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
    blocked: tuple[str, bool] | None = None,
    needs_human: str | None = None,
) -> ExampleOutcome:
    if last.passed and first.passed:
        status = ExampleStatus.PASSED
    elif last.passed:
        status = ExampleStatus.DEPENDENT if caused_by and not fixes else ExampleStatus.FIXED
    else:
        status = ExampleStatus.FAILING
    failing = status == ExampleStatus.FAILING
    blocked_by, knock_on = blocked or (None, False)
    if failing and blocked_by and knock_on:
        note = f"not auto-fixed: {blocked_by} failed in this run, so what it reads was never created"
    elif failing and blocked_by and not note:
        note = f"not auto-fixed: it runs after {blocked_by}, which still fails and may be the cause"
    needs_human = needs_human if failing else None
    if needs_human:
        note = f"needs a human: {needs_human}"
    return ExampleOutcome(
        file=last.example.file,
        label=last.example.label,
        line=last.example.line,
        status=status,
        expected_status=first.example.expected_status,
        actual_status=first.actual_status,
        final_expected_status=last.example.expected_status,
        final_actual_status=last.actual_status,
        failures=last.failures if failing else first.failures,
        warnings=last.warnings,
        fixes=fixes if status in (ExampleStatus.FIXED, ExampleStatus.FAILING) else [],
        note=note if failing else None,
        caused_by=caused_by if status == ExampleStatus.DEPENDENT else None,
        blocked_by=blocked_by if failing else None,
        knock_on=knock_on if failing else False,
        needs_human=needs_human,
    )


def execute_docs(
    files: dict[str, str],
    docs: dict[str, str],
    fixer: ExampleFixer | None,
    limits: SandboxLimits = SandboxLimits(),
    progress: Progress | None = None,
    python: str | None = None,
) -> ExecutionReport:
    sandbox = Sandbox(
        files,
        cache_dir=limits.cache_dir,
        python=python,
        startup_timeout=limits.startup_timeout,
        install_timeout=limits.install_timeout,
        user=limits.user,
    )
    return DocRunner(sandbox, fixer, request_timeout=limits.request_timeout, progress=progress).execute(docs, files)


def execute_all_docs(
    docs: list[DocFile],
    files: dict[str, str],
    proposals: DocProposals,
    min_confidence: float,
    fixer: ExampleFixer | None,
    limits: SandboxLimits = SandboxLimits(),
    progress: Progress | None = None,
    python: str | None = None,
) -> ExecutionReport:
    """Run the examples in every doc file, as the proposals leave it.

    Unchanged files run too: a PR that only changes runtime behavior (a status code, a response
    field) breaks examples that static analysis has no reason to look at.
    """
    return execute_docs(files, proposed_docs(docs, proposals, min_confidence), fixer, limits, progress, python)
