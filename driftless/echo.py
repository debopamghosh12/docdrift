"""Keep documented responses consistent with a fixed request.

When a fix changes a value in an example's request body (`"body": "Milk"` -> `"Tea"`), later
documented responses in the same file that echo it (the `GET /notes/1` after the `POST`) still
show the old value. This updates them deterministically, with no LLM. A documented response field
is changed only when all of these hold:

- its key is a key whose value the fix changed in the request body,
- the docs show the old request value there,
- the live response, run with the fixed request, returns the new value at the same place,
- and every documented `"key": old` in that response is such a field (otherwise none is touched).

Only the documented result is edited, never a request block: a later request that sends the same
value on purpose is its own example.
"""

import json
import re
from typing import Any

from driftless.examples import Example
from driftless.executor import ExampleRun

Scalar = str | int | float | bool
Change = tuple[str, Scalar, Scalar]  # (key, old value, new value)


def changed_request_values(before: Example, after: Example) -> list[Change]:
    """The scalar values a fix changed in the request's JSON body, by key."""
    changes: list[Change] = []
    _diff(_json(before.body), _json(after.body), None, changes)
    return changes


def echo_request_values(text: str, fixed_index: int, changes: list[Change], runs: list[ExampleRun]) -> tuple[str, list[str]]:
    """`text` with the changed values updated in documented responses from example `fixed_index`
    on (`runs` is that text's run with the fixed request), and one note per updated field."""
    lines = text.split("\n")
    notes: list[str] = []
    for run in runs:
        example = run.example
        if example.index < fixed_index or not example.has_expected_json or example.request_end_line <= example.start_line:
            continue
        actual = _json(run.actual_body)
        for key, old, new in changes:
            documented = _count(example.expected_json, key, old)
            if not documented or _echoes(example.expected_json, actual, key, old, new) != documented:
                continue
            pattern = re.compile(rf'("{re.escape(key)}"\s*:\s*){re.escape(_literal(old))}(?=\s*(?:[,}}\]]|$))')
            region = lines[example.request_end_line : example.end_line]
            if sum(len(pattern.findall(line)) for line in region) != documented:
                continue  # formatted in a way we can't edit safely
            replacement = _literal(new)
            region = [pattern.sub(lambda m: m.group(1) + replacement, line) for line in region]
            lines[example.request_end_line : example.end_line] = region
            notes.append(f"{example.label} (line {example.line}): `{key}` {_literal(old)} → {replacement}")
    return "\n".join(lines), notes


def _diff(old: Any, new: Any, key: str | None, out: list[Change]) -> None:
    if isinstance(old, dict) and isinstance(new, dict):
        for k in old.keys() & new.keys():
            _diff(old[k], new[k], k, out)
    elif isinstance(old, list) and isinstance(new, list):
        for a, b in zip(old, new):
            _diff(a, b, key, out)
    elif key is not None and _is_scalar(old) and type(old) is type(new) and old != new:
        out.append((key, old, new))


def _count(documented: Any, key: str, old: Scalar) -> int:
    """How many `key: old` fields the documented JSON has."""
    if isinstance(documented, dict):
        return sum(
            (1 if k == key and _same(v, old) else 0) + _count(v, key, old) for k, v in documented.items()
        )
    if isinstance(documented, list):
        return sum(_count(item, key, old) for item in documented)
    return 0


def _echoes(documented: Any, actual: Any, key: str, old: Scalar, new: Scalar) -> int:
    """How many `key: old` fields the actual response has as `key: new`, at the same place."""
    if isinstance(documented, dict) and isinstance(actual, dict):
        total = 0
        for k, v in documented.items():
            if k not in actual:
                continue
            if k == key and _same(v, old) and _same(actual[k], new):
                total += 1
            total += _echoes(v, actual[k], key, old, new)
        return total
    if isinstance(documented, list) and isinstance(actual, list):
        return sum(_echoes(d, a, key, old, new) for d, a in zip(documented, actual))
    return 0


def _same(value: Any, expected: Scalar) -> bool:
    return type(value) is type(expected) and value == expected


def _is_scalar(value: Any) -> bool:
    return isinstance(value, (str, int, float, bool))


def _literal(value: Scalar) -> str:
    return json.dumps(value, ensure_ascii=False)


def _json(text: str | None) -> Any:
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None
