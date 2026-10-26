"""Executable docs: run every example in a set of doc files against the real app, fix failures.

Per doc file: start a fresh app, run its examples in order, and if any fail (and an LLM is
available) ask for a fix of each failing example from its actual request/response, splice the
fixes in, and re-run the whole file on a fresh app, up to `max_attempts` times. Each example
ends up 🧪 passed, 🔁 fixed after running, or ❌ still failing.
"""

import logging
from collections.abc import Callable
from pathlib import Path

from driftless.doc_editor import apply_proposals
from driftless.doc_index import is_doc_path
from driftless.example_fixer import ExampleFixer, route_code
from driftless.examples import Example, documented_env_defaults, extract_examples
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
        request_timeout: float = 10.0,
        progress: Progress | None = None,
    ) -> None:
        self._sandbox = sandbox
        self._fixer = fixer
        self._max_attempts = max_attempts
        self._request_timeout = request_timeout
        self._progress = progress or (lambda message: log.info(message))

    def execute(self, docs: dict[str, str], files: dict[str, str]) -> ExecutionReport:
        """Run `docs` ({path: text}); `files` is the repo snapshot the app is started from."""
        report = ExecutionReport()
        runnable = {path: text for path, text in sorted(docs.items()) if extract_examples(path, text)}
        if not runnable:
            return report
        env = env_defaults({**{p: t for p, t in files.items() if is_doc_path(p)}, **docs})
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
        self, box: Sandbox, path: str, text: str, files: dict[str, str], env: dict[str, str], report: ExecutionReport
    ) -> None:
        first = self._run(box, path, text, env)
        runs = first
        fixes: dict[int, list[str]] = {}
        notes: dict[int, str] = {}
        for _ in range(self._max_attempts):
            failing = [r for r in runs if not r.passed]
            if not failing or self._fixer is None:
                break
            text, applied = self._fix(text, runs, failing, files, fixes, notes)
            if not applied:
                break
            runs = self._run(box, path, text, env)
            if len(runs) != len(first):  # a fix changed the example count; stop rather than mislabel
                report.errors.append(f"{path}: a fix changed the number of examples; kept it unverified")
                break
        report.contents[path] = text
        for before, after in zip(first, runs):
            report.outcomes.append(_outcome(before, after, fixes.get(before.example.index, []), notes.get(before.example.index)))

    def _run(self, box: Sandbox, path: str, text: str, env: dict[str, str]) -> list[ExampleRun]:
        examples = extract_examples(path, text)
        with box.serve(env) as base_url:
            return run_examples(examples, base_url, timeout=self._request_timeout)

    def _fix(
        self,
        text: str,
        runs: list[ExampleRun],
        failing: list[ExampleRun],
        files: dict[str, str],
        fixes: dict[int, list[str]],
        notes: dict[int, str],
    ) -> tuple[str, bool]:
        assert self._fixer is not None
        lines = text.split("\n")
        replacements: list[tuple[Example, list[str]]] = []
        for run in failing:
            example = run.example
            if _overlaps_another(example, [r.example for r in runs]):
                notes[example.index] = "shares a code block with another example; not auto-fixed"
                continue
            snippet = "\n".join(lines[example.start_line : example.end_line])
            try:
                reply = self._fixer.fix(run, snippet, route_code(files, example))
            except LLMError as exc:
                notes[example.index] = f"fix failed: {exc}"
                continue
            if not reply.fixed or not reply.replacement or reply.replacement.strip() == snippet.strip():
                notes[example.index] = f"model could not fix it: {reply.reason}"
                continue
            fixes.setdefault(example.index, []).append(reply.reason)
            notes.pop(example.index, None)
            replacements.append((example, reply.replacement.strip("\n").split("\n")))
        for example, new_lines in sorted(replacements, key=lambda r: r[0].start_line, reverse=True):
            lines[example.start_line : example.end_line] = new_lines
        return "\n".join(lines), bool(replacements)


def env_defaults(docs: dict[str, str]) -> dict[str, str]:
    """Env var defaults documented anywhere in the docs (tables with a "Default" column)."""
    defaults: dict[str, str] = {}
    for _, text in sorted(docs.items()):
        defaults.update(documented_env_defaults(text))
    return defaults


def _overlaps_another(example: Example, examples: list[Example]) -> bool:
    return any(
        other.index != example.index and other.start_line < example.end_line and example.start_line < other.end_line
        for other in examples
    )


def _outcome(first: ExampleRun, last: ExampleRun, fixes: list[str], note: str | None) -> ExampleOutcome:
    if last.passed:
        status = ExampleStatus.PASSED if first.passed else ExampleStatus.FIXED
    else:
        status = ExampleStatus.FAILING
    return ExampleOutcome(
        file=last.example.file,
        label=last.example.label,
        line=last.example.line,
        status=status,
        expected_status=first.example.expected_status,
        actual_status=first.actual_status,
        final_expected_status=last.example.expected_status,
        final_actual_status=last.actual_status,
        failures=first.failures if status == ExampleStatus.FIXED else last.failures,
        warnings=last.warnings,
        fixes=fixes if status != ExampleStatus.PASSED else [],
        note=note if status == ExampleStatus.FAILING else None,
    )


def execute_docs(
    files: dict[str, str],
    docs: dict[str, str],
    fixer: ExampleFixer | None,
    cache_dir: Path = Path(".driftless_cache"),
    startup_timeout: float = 30.0,
    progress: Progress | None = None,
) -> ExecutionReport:
    sandbox = Sandbox(files, cache_dir=cache_dir, startup_timeout=startup_timeout)
    return DocRunner(sandbox, fixer, progress=progress).execute(docs, files)


def execute_updated_docs(
    docs: list[DocFile],
    files: dict[str, str],
    proposals: DocProposals,
    min_confidence: float,
    fixer: ExampleFixer | None,
    cache_dir: Path = Path(".driftless_cache"),
    startup_timeout: float = 30.0,
    progress: Progress | None = None,
) -> ExecutionReport | None:
    """Run the examples in every doc file the proposals changed; None when nothing changed."""
    updated = apply_proposals(docs, proposals, min_confidence)
    if not updated:
        return None
    return execute_docs(files, updated, fixer, cache_dir, startup_timeout, progress)
