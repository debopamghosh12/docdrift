"""LLM call that fixes one failing doc example from its actual request/response."""

import json
import re
from typing import Any

from driftless.examples import Example, extract_examples
from driftless.execution import ExampleFix
from driftless.executor import ExampleRun, is_flexible_key, json_type
from driftless.llm import JsonLLM
from driftless.prompts import render_prompt

MAX_CODE_CHARS = 6000
_ROUTE_DECORATOR_RE = re.compile(r"@\w+\.(get|post|put|patch|delete|head|options|api_route)\(")
_WRAPPER_RE = re.compile(r"^\s*</?(example|snippet|replacement)>\s*$")
_JSON_BLOCK_RE = re.compile(r"^ {0,3}```json[^\n]*\n(.*?)\n {0,3}```", re.DOTALL | re.MULTILINE)


class ExampleFixer:
    def __init__(self, llm: JsonLLM) -> None:
        self._llm = llm
        self._system = render_prompt("system")

    def fix(self, run: ExampleRun, snippet: str, code: str) -> ExampleFix:
        example = run.example
        prompt = render_prompt(
            "fix_example",
            file=example.file,
            snippet=snippet,
            request=describe_request(example),
            actual_status=str(run.actual_status) if run.actual_status is not None else "(no response)",
            actual_body=run.actual_body or "(empty)",
            failures="\n".join(f"- {failure}" for failure in run.failures),
            code=code or "(not available)",
        )

        def check(reply: ExampleFix) -> None:
            if reply.fixed:
                check_replacement(reply.replacement or "", example.file)

        reply = self._llm.complete(self._system, prompt, ExampleFix, check)
        if reply.fixed and reply.replacement:
            reply.replacement = tidy_replacement(snippet, reply.replacement)
        return reply


def tidy_replacement(snippet: str, replacement: str) -> str:
    """Undo cosmetic churn in a fix: wrapper tags, live ids/timestamps, reformatted JSON.

    Ids and timestamps from the live run (`"id": 1`, `"created_at": "2026-..."`) are put back to
    the documented values, and a JSON block that is then unchanged keeps its original text; a
    changed one is re-serialized in the original's layout (one line, or indented).
    """
    lines = replacement.strip("\n").split("\n")
    while lines and _WRAPPER_RE.match(lines[0]):
        lines = lines[1:]
    while lines and _WRAPPER_RE.match(lines[-1]):
        lines = lines[:-1]
    text = "\n".join(lines)
    old_blocks = _JSON_BLOCK_RE.findall(snippet)
    new_blocks = list(_JSON_BLOCK_RE.finditer(text))
    if len(old_blocks) != len(new_blocks):
        return text
    for old_text, match in reversed(list(zip(old_blocks, new_blocks))):
        try:
            old, new = json.loads(old_text), json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        restored = restore_flexible(old, new)
        if restored == old:
            body = old_text
        elif "\n" in old_text.strip():
            body = json.dumps(restored, indent=2, ensure_ascii=False)
        else:
            body = json.dumps(restored, ensure_ascii=False)
        text = text[: match.start(1)] + body + text[match.end(1) :]
    return text


def restore_flexible(documented: Any, actual: Any) -> Any:
    """`actual` with documented values kept for id/timestamp keys present in both."""
    if isinstance(documented, dict) and isinstance(actual, dict):
        return {
            key: (
                documented[key]
                if key in documented and is_flexible_key(key) and json_type(documented[key]) == json_type(value)
                and not isinstance(value, dict | list)
                else restore_flexible(documented.get(key), value)
            )
            for key, value in actual.items()
        }
    if isinstance(documented, list) and isinstance(actual, list):
        return [restore_flexible(documented[i] if i < len(documented) else None, v) for i, v in enumerate(actual)]
    return actual


def check_replacement(replacement: str, path: str) -> None:
    found = extract_examples(path, replacement)
    if len(found) != 1:
        raise ValueError(
            f"replacement must contain exactly one runnable request (curl or http block), found {len(found)}"
        )


def describe_request(example: Example) -> str:
    headers = "".join(f"\n{name}: {value}" for name, value in example.headers.items())
    body = f"\n\n{example.body}" if example.body is not None else ""
    return f"{example.method} {example.path}{headers}{body}"


def route_code(files: dict[str, str], example: Example) -> str:
    """Python files defining routes under the example's first path segment, size-capped."""
    segment = "/" + example.path.split("?", 1)[0].strip("/").split("/", 1)[0]
    picked: list[str] = []
    size = 0
    for path, content in sorted(files.items()):
        if not path.endswith(".py") or not _ROUTE_DECORATOR_RE.search(content):
            continue
        if f'"{segment}' not in content and f"'{segment}" not in content:
            continue
        chunk = f"# {path}\n{content}"
        if size + len(chunk) > MAX_CODE_CHARS:
            chunk = chunk[: max(0, MAX_CODE_CHARS - size)]
        picked.append(chunk)
        size += len(chunk)
        if size >= MAX_CODE_CHARS:
            break
    return "\n\n".join(picked)
