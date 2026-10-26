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


class ExampleStatus(str, Enum):
    PASSED = "passed"
    FIXED = "fixed"  # failed, then passed after an LLM fix and a re-run
    FAILING = "failing"


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
