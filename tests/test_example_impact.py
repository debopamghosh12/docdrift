from driftless.example_impact import broken_phrase, consequence_sentence, example_impact, impact_sentence
from driftless.execution import ExampleOutcome, ExampleStatus


def outcome(file: str, label: str, status: ExampleStatus, **extra: object) -> ExampleOutcome:
    return ExampleOutcome(file=file, label=label, line=1, status=status, **extra)


def notes_pr_outcomes() -> list[ExampleOutcome]:
    """driftless-demo #6: POST /notes is rejected (422), so the GET and DELETE after it 404, in two files."""
    outcomes = []
    for file in ("README.md", "docs/api.md"):
        outcomes += [
            outcome(file, "GET /health", ExampleStatus.PASSED),
            outcome(file, "POST /notes", ExampleStatus.FIXED),
            outcome(file, "GET /notes/1", ExampleStatus.DEPENDENT, caused_by="POST /notes (line 3)"),
            outcome(file, "DELETE /notes/1", ExampleStatus.DEPENDENT, caused_by="POST /notes (line 3)"),
        ]
    return outcomes


def test_root_causes_are_counted_once_per_label() -> None:
    impact = example_impact(notes_pr_outcomes())
    assert impact.labels == ["POST /notes"] and impact.files == ["README.md", "docs/api.md"]
    assert len(impact.dependent) == 4 and impact.blocked == []
    assert impact_sentence(impact, "This PR") == (
        "This PR breaks 1 documented example (`POST /notes`) in 2 files; "
        "4 later examples failed as a result and pass once it's fixed."
    )


def test_wording_for_one_file_many_labels_and_blocked_examples() -> None:
    outcomes = [
        outcome("README.md", label, ExampleStatus.FAILING) for label in ("POST /a", "POST /b", "POST /c", "POST /d")
    ] + [outcome("README.md", "GET /a/1", ExampleStatus.FAILING, blocked_by="POST /d (line 1)")]
    impact = example_impact(outcomes)
    assert broken_phrase(impact) == "4 documented examples (`POST /a`, `POST /b`, `POST /c` and 1 more) in `README.md`"
    assert consequence_sentence(impact) == "1 later example still fails and may only be failing because of it."


def test_no_consequences_ends_the_sentence() -> None:
    impact = example_impact([outcome("README.md", "POST /a", ExampleStatus.FIXED)])
    assert consequence_sentence(impact) == ""
    assert impact_sentence(impact, "#6") == "#6 breaks 1 documented example (`POST /a`) in `README.md`."
    assert not example_impact([outcome("README.md", "GET /a", ExampleStatus.PASSED)])
