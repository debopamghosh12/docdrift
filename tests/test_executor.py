from driftless.examples import Example
from driftless.executor import compare, is_flexible_key


def example(status: int | None = 200, payload: object = None, has_json: bool = True) -> Example:
    return Example(
        file="README.md", index=0, method="GET", path="/x", expected_status=status,
        expected_json=payload, has_expected_json=has_json, start_line=0, end_line=3, source="",
    )


def test_status_mismatch_fails() -> None:
    failures, _ = compare(example(201, has_json=False), 200, "")
    assert failures == ["status is 200, docs say 201"]


def test_ids_and_timestamps_are_flexible_other_values_warn() -> None:
    documented = {"id": 7, "name": "Pen", "created_at": "2024-01-01", "price": 1.5}
    failures, warnings = compare(
        example(payload=documented), 200, '{"id": 1, "name": "Ink", "created_at": "2026-10-26", "price": 2}'
    )
    assert failures == []
    assert warnings == ['$.name: docs show "Pen", got "Ink"', "$.price: docs show 1.5, got 2"]


def test_structure_differences_fail() -> None:
    failures, _ = compare(example(payload={"id": 1, "title": "x"}), 200, '{"id": "1", "body": "x"}')
    assert failures == [
        "$: missing key(s) 'title'",
        "$: undocumented key(s) 'body'",
        "$.id: docs show number, got string",
    ]


def test_list_length_is_a_warning_and_non_json_fails() -> None:
    failures, warnings = compare(example(payload=[{"id": 1}]), 200, "[]")
    assert failures == [] and warnings == ["$: docs show 1 item(s), got 0"]
    failures, _ = compare(example(payload={"a": 1}), 200, "<html>")
    assert failures == ["docs show a JSON response, but the response is not JSON"]


def test_undocumented_status_must_be_success() -> None:
    assert compare(example(None, has_json=False), 404, "")[0] == [
        "status is 404 (no status documented, expected success)"
    ]


def test_flexible_keys() -> None:
    assert all(map(is_flexible_key, ["id", "note_id", "userId", "created_at", "updatedAt", "timestamp"]))
    assert not any(map(is_flexible_key, ["valid", "paid", "title", "identity"]))
