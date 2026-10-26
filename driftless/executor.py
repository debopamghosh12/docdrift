"""Run extracted examples in order against a live app and compare with the documented result.

Requests share one app instance, in document order, so a POST can create what a later GET
reads. A status mismatch fails; documented JSON must match the actual JSON in structure (same
keys, same value types). Ids and timestamps may differ freely; other value differences and
list-length differences are warnings, since they usually reflect state rather than wrong docs.
"""

import json
import re
from typing import Any

import httpx
from pydantic import BaseModel, Field

from driftless.examples import Example

MAX_BODY_CHARS = 2000
# Matched against snake_case keys (camelCase is converted first).
_FLEXIBLE_KEY_RE = re.compile(
    r"(^|_)(id|ids|uuid|guid|etag|token)$|(^|_)(created|updated|deleted|modified)(_on)?$|_at$|"
    r"(^|_)(timestamp|time|date|datetime|expires)$"
)


class ExampleRun(BaseModel):
    example: Example
    actual_status: int | None = None
    actual_body: str = ""
    failures: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.failures


def run_examples(examples: list[Example], base_url: str, timeout: float = 10.0) -> list[ExampleRun]:
    with httpx.Client(base_url=base_url, timeout=timeout, follow_redirects=False) as client:
        return [run_example(client, example) for example in examples]


def run_example(client: httpx.Client, example: Example) -> ExampleRun:
    try:
        response = client.request(
            example.method,
            example.path,
            headers=example.headers,
            content=example.body.encode("utf-8") if example.body is not None else None,
        )
    except httpx.HTTPError as exc:
        return ExampleRun(example=example, failures=[f"request failed: {type(exc).__name__}: {exc}"])
    run = ExampleRun(example=example, actual_status=response.status_code, actual_body=response.text[:MAX_BODY_CHARS])
    run.failures, run.warnings = compare(example, response.status_code, response.text)
    return run


def compare(example: Example, status: int, body: str) -> tuple[list[str], list[str]]:
    failures: list[str] = []
    warnings: list[str] = []
    if example.expected_status is not None:
        if status != example.expected_status:
            failures.append(f"status is {status}, docs say {example.expected_status}")
    elif status >= 400:
        failures.append(f"status is {status} (no status documented, expected success)")
    if example.has_expected_json and not failures:
        try:
            actual = json.loads(body)
        except json.JSONDecodeError:
            failures.append("docs show a JSON response, but the response is not JSON")
        else:
            compare_json(example.expected_json, actual, "$", failures, warnings)
    return failures, warnings


def compare_json(expected: Any, actual: Any, where: str, failures: list[str], warnings: list[str], flexible: bool = False) -> None:
    if json_type(expected) != json_type(actual):
        failures.append(f"{where}: docs show {json_type(expected)}, got {json_type(actual)}")
        return
    if isinstance(expected, dict):
        missing = [k for k in expected if k not in actual]
        extra = [k for k in actual if k not in expected]
        if missing:
            failures.append(f"{where}: missing key(s) {', '.join(map(repr, missing))}")
        if extra:
            failures.append(f"{where}: undocumented key(s) {', '.join(map(repr, extra))}")
        for key in expected:
            if key in actual:
                compare_json(expected[key], actual[key], f"{where}.{key}", failures, warnings, is_flexible_key(key))
    elif isinstance(expected, list):
        if len(expected) != len(actual):
            warnings.append(f"{where}: docs show {len(expected)} item(s), got {len(actual)}")
        for i, (e, a) in enumerate(zip(expected, actual)):
            compare_json(e, a, f"{where}[{i}]", failures, warnings, flexible)
    elif expected != actual and not flexible:
        warnings.append(f"{where}: docs show {_short(expected)}, got {_short(actual)}")


def is_flexible_key(key: str) -> bool:
    """Ids and timestamps legitimately differ between the docs and a live run."""
    snake = re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", key).lower()
    return bool(_FLEXIBLE_KEY_RE.search(snake))


def json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int | float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object"


def _short(value: Any) -> str:
    text = json.dumps(value)
    return text if len(text) <= 60 else text[:57] + "..."
