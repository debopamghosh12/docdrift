"""Root causes vs. knock-on failures of executed examples, as headline text.

A rejected `POST /notes` makes the `GET /notes/1` and `DELETE /notes/1` after it 404. That is one
broken example, not three: headlines count root causes (fixed, or failing with nothing earlier to
blame) and mention the rest as consequences.
"""

from dataclasses import dataclass

from driftless.execution import ExampleOutcome, ExampleStatus

MAX_LABELS = 3


@dataclass(frozen=True)
class ExampleImpact:
    roots: list[ExampleOutcome]  # broken in their own right
    dependent: list[ExampleOutcome]  # passed once an earlier example was fixed
    blocked: list[ExampleOutcome]  # still failing after an earlier example that still fails

    @property
    def labels(self) -> list[str]:
        """Distinct broken examples, in order: the same `POST /notes` in two files is one."""
        return list(dict.fromkeys(o.label for o in self.roots))

    @property
    def files(self) -> list[str]:
        return list(dict.fromkeys(o.file for o in self.roots))

    def __bool__(self) -> bool:
        return bool(self.roots or self.dependent or self.blocked)


def example_impact(outcomes: list[ExampleOutcome]) -> ExampleImpact:
    return ExampleImpact(
        roots=[o for o in outcomes if o.root_cause],
        dependent=[o for o in outcomes if o.status == ExampleStatus.DEPENDENT],
        blocked=[o for o in outcomes if o.status == ExampleStatus.FAILING and o.blocked_by],
    )


def broken_phrase(impact: ExampleImpact) -> str:
    """'1 documented example (`POST /notes`) in 2 files'."""
    labels = impact.labels
    shown = ", ".join(f"`{label}`" for label in labels[:MAX_LABELS])
    if len(labels) > MAX_LABELS:
        shown += f" and {len(labels) - MAX_LABELS} more"
    files = impact.files
    where = f"`{files[0]}`" if len(files) == 1 else f"{len(files)} files"
    return f"{_plural(len(labels), 'documented example')} ({shown}) in {where}"


def consequence_sentence(impact: ExampleImpact) -> str:
    """'4 later examples failed as a result and pass once it's fixed.' ('' when there are none)."""
    it = "it's" if len(impact.labels) == 1 else "they're"
    parts = []
    if impact.dependent:
        n = len(impact.dependent)
        parts.append(f"{_plural(n, 'later example')} failed as a result and {_verb(n, 'pass')} once {it} fixed")
    if impact.blocked:
        n = len(impact.blocked)
        parts.append(f"{_plural(n, 'later example')} still {_verb(n, 'fail')} and may only be failing because of it")
    if not parts:
        return ""
    sentence = "; ".join(parts)
    return sentence[0].upper() + sentence[1:] + "."


def impact_sentence(impact: ExampleImpact, subject: str) -> str:
    """'This PR breaks 1 documented example (`POST /notes`) in 2 files; 4 later examples ...'."""
    sentence = f"{subject} breaks {broken_phrase(impact)}"
    consequence = consequence_sentence(impact)
    if consequence:
        sentence += "; " + consequence[0].lower() + consequence[1:]
        return sentence
    return sentence + "."


def _verb(n: int, verb: str) -> str:
    """Present tense agreeing with `n` ('pass' -> 'passes' for 1)."""
    if n != 1:
        return verb
    return verb + ("es" if verb.endswith("s") else "s")


def _plural(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"
