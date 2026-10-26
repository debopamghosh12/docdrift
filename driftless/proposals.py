"""Models for LLM replies and for proposed, verified documentation changes."""

from enum import Enum

from pydantic import BaseModel, Field, model_validator

from driftless.execution import ExecutionReport
from driftless.models import DocSection, MissingDoc


class SectionVerdict(BaseModel):
    """The JSON the model must return for a candidate section."""

    stale: bool
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str
    updated_section: str | None = None

    @model_validator(mode="after")
    def _update_required_when_stale(self) -> "SectionVerdict":
        if self.stale and not (self.updated_section or "").strip():
            raise ValueError("updated_section must be a non-empty string when stale is true")
        return self


class BatchSectionVerdict(SectionVerdict):
    id: str


class BatchVerdicts(BaseModel):
    """The JSON the model must return when reviewing several sections of one file at once."""

    sections: list[BatchSectionVerdict]


class NewSectionProposal(BaseModel):
    """The JSON the model must return for a missing-docs item."""

    file: str
    after_heading: str | None = None
    section: str = Field(min_length=1)
    reason: str
    confidence: float = Field(ge=0.0, le=1.0)


class VerificationStatus(str, Enum):
    VERIFIED = "verified"
    FIXED_ON_RETRY = "fixed_on_retry"
    DROPPED = "dropped"


class Verification(BaseModel):
    status: VerificationStatus
    failures: list[str] = Field(default_factory=list)  # first attempt's failures, if any
    retry_failures: list[str] = Field(default_factory=list)  # why a retry was dropped
    note: str | None = None
    flags: list[str] = Field(default_factory=list)
    restored_lines: int = 0

    @property
    def applied(self) -> bool:
        return self.status != VerificationStatus.DROPPED


class StaleFinding(BaseModel):
    section: DocSection
    matched_terms: list[str]
    verdict: SectionVerdict
    verification: Verification | None = None  # None when not stale or below confidence


class Insertion(BaseModel):
    item: MissingDoc
    proposal: NewSectionProposal
    verification: Verification | None = None


class CoveredItem(BaseModel):
    item: MissingDoc
    file: str


class DocProposals(BaseModel):
    findings: list[StaleFinding] = Field(default_factory=list)
    insertions: list[Insertion] = Field(default_factory=list)
    covered: list[CoveredItem] = Field(default_factory=list)  # already documented by a rewrite
    errors: list[str] = Field(default_factory=list)
    execution: ExecutionReport | None = None  # examples run in all doc files (fixes included)
