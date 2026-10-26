"""Models for executed doc examples: the LLM's fix reply and the per-example outcome report."""

from enum import Enum

from pydantic import BaseModel, Field, model_validator


class ExampleFix(BaseModel):
    """The JSON the model must return for a failing example."""

    fixed: bool
    target: str = Field(description="request | response | both | none")
    reason: str
    replacement: str | None = None

    @model_validator(mode="after")
    def _replacement_required_when_fixed(self) -> "ExampleFix":
        if self.fixed and not (self.replacement or "").strip():
            raise ValueError("replacement must be a non-empty string when fixed is true")
        return self


def is_success(status: int | None) -> bool:
    """A 2xx status, or none documented (which the executor reads as "expect success")."""
    return status is None or 200 <= status < 300


class ExampleStatus(str, Enum):
    PASSED = "passed"
    FIXED = "fixed"  # failed, then passed after an LLM fix and a re-run
    FAILING = "failing"
    # Failed only because an earlier example in the same file failed; passed once that was fixed.
    DEPENDENT = "dependent"


class ExampleOutcome(BaseModel):
    file: str
    label: str  # "POST /notes"
    line: int  # 1-based, in the final file
    status: ExampleStatus
    expected_status: int | None = None  # as documented on the first run
    actual_status: int | None = None  # first run
    final_expected_status: int | None = None
    final_actual_status: int | None = None
    failures: list[str] = Field(default_factory=list)  # first run's for fixed, last run's otherwise
    warnings: list[str] = Field(default_factory=list)
    fixes: list[str] = Field(default_factory=list)  # reason of every applied fix
    note: str | None = None  # why a failing example wasn't fixed
    pre_existing: bool = False  # pre-merge: already failed at the PR's base commit
    caused_by: str | None = None  # dependent: the earlier example whose fix made it pass, "POST /notes (line 12)"
    blocked_by: str | None = None  # failing, not fixed: an earlier example still fails and may be the cause
    # blocked_by is certain: a 404 on a resource whose create (POST/PUT) earlier in the file failed.
    knock_on: bool = False
    # Failing happy path no valid request could make succeed: the reason a human must look at it.
    needs_human: str | None = None

    @property
    def root_cause(self) -> bool:
        """Broken in its own right: fixed, or still failing with no earlier failure to blame."""
        return self.status == ExampleStatus.FIXED or (self.status == ExampleStatus.FAILING and not self.blocked_by)


class ExecutionReport(BaseModel):
    outcomes: list[ExampleOutcome] = Field(default_factory=list)
    contents: dict[str, str] = Field(default_factory=dict)  # final text of every executed file
    errors: list[str] = Field(default_factory=list)
    app: str | None = None  # "app.main:app"

    @property
    def total(self) -> int:
        return len(self.outcomes)

    @property
    def passing(self) -> int:
        return sum(1 for o in self.outcomes if o.status != ExampleStatus.FAILING)

    @property
    def fixed(self) -> list[ExampleOutcome]:
        return [o for o in self.outcomes if o.status == ExampleStatus.FIXED]

    @property
    def failing(self) -> list[ExampleOutcome]:
        return [o for o in self.outcomes if o.status == ExampleStatus.FAILING]

    @property
    def dependent(self) -> list[ExampleOutcome]:
        return [o for o in self.outcomes if o.status == ExampleStatus.DEPENDENT]

    @property
    def root_causes(self) -> list[ExampleOutcome]:
        return [o for o in self.outcomes if o.root_cause]
