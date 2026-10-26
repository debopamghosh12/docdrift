"""Check proposed doc text against the code at the merge commit before it is trusted.

Checks (all deterministic, no LLM):
1. signatures shown in docs match the real definition (names, order, defaults)
2. functions/classes/constants/env vars/CLI flags in code exist in the repo
3. documented `METHOD /path` routes exist
4. fenced Python blocks parse
5. `NAME=value` example settings use values from the code or the original docs
6. cosmetic churn is undone; large unrelated rewrites are flagged
"""

import ast
import builtins
import keyword
import re
import textwrap
from dataclasses import dataclass, field

from docdrift.code_index import CodeIndex, FunctionFact
from docdrift.doc_mentions import PY_LANGS, DocSignature, extract_mentions
from docdrift.minimal_diff import restore_formatting, unrelated_change_flag

_JS_GLOBALS = {
    "console", "JSON", "Promise", "Object", "Array", "Math", "Date", "Error", "fetch", "require",
    "process", "setTimeout", "Number", "String", "Boolean", "Map", "Set", "Symbol", "parseInt",
    "parseFloat", "encodeURIComponent", "async", "await", "function", "typeof", "new",
}
KNOWN_GLOBALS = frozenset(dir(builtins)) | frozenset(keyword.kwlist) | _JS_GLOBALS
# `APP_PORT=9000` in a shell example or `.env` snippet.
_ASSIGNMENT_RE = re.compile(r"(?<![\w$-])([A-Z][A-Z0-9_]{2,})=(\"[^\"]*\"|'[^']*'|[^\s`'\";&|]+)")


@dataclass
class VerifyReport:
    text: str  # the rewrite after cosmetic churn was undone
    failures: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    restored_lines: int = 0

    @property
    def ok(self) -> bool:
        return not self.failures


class Verifier:
    def __init__(self, code: CodeIndex, changed_names: list[str]) -> None:
        self._code = code
        self._changed_names = changed_names

    def check(self, original: str, updated: str) -> VerifyReport:
        text, restored = restore_formatting(original, updated) if original else (updated, 0)
        failures = [
            *check_signatures(text, self._code),
            *check_symbols(original, text, self._code),
            *check_routes(original, text, self._code),
            *check_example_values(original, text, self._code),
            *check_code_blocks(original, text),
        ]
        flag = unrelated_change_flag(original, text, self._changed_names) if original else None
        return VerifyReport(text=text, failures=failures, flags=[flag] if flag else [], restored_lines=restored)


def check_signatures(text: str, code: CodeIndex) -> list[str]:
    failures: list[str] = []
    for signature in extract_mentions(text).signatures:
        facts = code.functions.get(signature.name)
        if not facts:
            continue  # unknown names are check_symbols' job
        problems = [(compare_signature(signature, fact), fact) for fact in facts]
        if any(not errors for errors, _ in problems):
            continue  # matches at least one definition with that name
        errors, fact = min(problems, key=lambda item: len(item[0]))
        failures += [f"{error} ({fact.file})" for error in errors]
    return failures


def compare_signature(doc: DocSignature, fact: FunctionFact) -> list[str]:
    name = doc.name
    code_names = [p.name for p in fact.params]
    by_name = {p.name: p for p in fact.params}
    errors: list[str] = []
    for param in doc.params:
        if param.name not in by_name and not fact.varargs:
            errors.append(
                f"{name}: parameter {param.name} does not exist in code; real signature is {fact.describe()}"
            )
    shown = [p.name for p in doc.params if p.name in by_name]
    if shown != sorted(shown, key=code_names.index):
        errors.append(f"{name}: parameter order is {', '.join(code_names)} in code, doc says {', '.join(shown)}")
    doc_names = {p.name for p in doc.params}
    for param in fact.params:
        if param.default is None and param.name not in doc_names:
            errors.append(f"{name}: required parameter {param.name} is missing from the doc")
    for param in doc.params:
        real = by_name.get(param.name)
        if real is None or param.default is None:
            continue
        if real.default is None:
            errors.append(f"{name}: {param.name} has no default in code, doc says {param.default}")
        elif not _same_default(param.default, real.default, real.resolved):
            errors.append(f"{name}: default of {param.name} is {real.default} in code, doc says {param.default}")
    return errors


def check_symbols(original: str, updated: str, code: CodeIndex) -> list[str]:
    """New code references must exist in the repo; references already in the original are exempt."""
    new, old = extract_mentions(updated), extract_mentions(original)
    failures: list[str] = []
    for name in sorted(new.calls - old.calls):
        if name not in KNOWN_GLOBALS and not code.knows(name):
            failures.append(f"`{name}()` is not defined anywhere in the repo at the merge commit")
    for name in sorted(new.classes - old.classes):
        if name not in KNOWN_GLOBALS and not code.knows(name):
            failures.append(f"class `{name}` does not exist in the repo at the merge commit")
    for name in sorted(new.constants - old.constants):
        if not code.knows(name):
            failures.append(f"`{name}` is not an env var or constant in the repo at the merge commit")
    if code.cli_flags:
        for flag in sorted(new.flags - old.flags - code.cli_flags):
            failures.append(f"CLI flag `{flag}` does not exist in the code")
    return failures


def check_example_values(original: str, updated: str, code: CodeIndex) -> list[str]:
    """`NAME=value` settings in examples must use a value from the code or the original docs."""
    old = set(_ASSIGNMENT_RE.findall(original))
    old_values = {_bare(value) for _, value in old}
    failures: list[str] = []
    for name, value in sorted(set(_ASSIGNMENT_RE.findall(updated)) - old):
        if name not in code.env_vars and name not in code.constants:
            continue  # not a setting of this repo; unknown names are check_symbols' job
        default = code.env_defaults.get(name) or code.constants.get(name)
        if _bare(value) in old_values or (default is not None and _bare(value) == _bare(default)):
            continue
        hint = f" (default in code: {_bare(default)})" if default is not None else ""
        failures.append(
            f"example value {name}={value} is not in the code or the original docs{hint}; "
            "use the code's value or leave the setting out of the example"
        )
    return failures


def check_routes(original: str, updated: str, code: CodeIndex) -> list[str]:
    if not code.routes:
        return []
    old = extract_mentions(original).routes
    failures: list[str] = []
    for method, path in sorted(extract_mentions(updated).routes - old):
        if any(m in (method, "ALL") and route_matches(path, p) for m, p in code.routes):
            continue
        methods = sorted(m for m, p in code.routes if route_matches(path, p))
        hint = f"; that path supports {', '.join(methods)}" if methods else "; no route has that path"
        failures.append(f"route {method} {path} does not exist in the code{hint}")
    return failures


def check_code_blocks(original: str, updated: str) -> list[str]:
    old_blocks = {b.text for b in extract_mentions(original).blocks}
    failures: list[str] = []
    for block in extract_mentions(updated).blocks:
        if block.lang not in PY_LANGS or block.text in old_blocks or ">>> " in block.text:
            continue
        try:
            ast.parse(textwrap.dedent(block.text))
        except SyntaxError as exc:
            first_line = block.text.strip().splitlines()[0] if block.text.strip() else ""
            failures.append(f"python code block does not parse (line {exc.lineno}: {exc.msg}): {first_line}")
    return failures


def route_matches(doc_path: str, template: str) -> bool:
    """Match a documented path (concrete or templated) against a code route template."""
    doc_parts = (doc_path.rstrip("/") or "/").split("/")
    code_parts = (template.rstrip("/") or "/").split("/")
    if len(doc_parts) != len(code_parts):
        return False
    return all(
        _is_param(d) or _is_param(c) or d == c for d, c in zip(doc_parts, code_parts)
    )


def _is_param(segment: str) -> bool:
    return segment[:1] in ("{", ":", "<")


def _same_default(doc: str, code: str, resolved: str | None) -> bool:
    accepted = {_canonical(code)} | ({_canonical(resolved)} if resolved is not None else set())
    return _canonical(doc) in accepted


def _canonical(value: str) -> str:
    value = value.strip().strip("`")
    try:
        return ast.unparse(ast.parse(value, mode="eval"))
    except SyntaxError:
        return value


def _bare(value: str) -> str:
    return value.strip("\"'")
