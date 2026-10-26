"""LLM-backed analysis: judge candidate sections and draft docs for missing items."""

from concurrent.futures import ThreadPoolExecutor

from pydantic import BaseModel, Field, model_validator

from docdrift.candidates import is_mentioned
from docdrift.evidence import (
    best_example,
    describe_changeset,
    doc_outline,
    file_anchors,
    relevant_hunks,
)
from docdrift.llm import JsonLLM, LLMError
from docdrift.models import Candidate, DocSection, MissingDoc
from docdrift.pipeline import AnalysisResult
from docdrift.prompts import render_prompt


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


class NewSectionProposal(BaseModel):
    """The JSON the model must return for a missing-docs item."""

    file: str
    after_heading: str | None = None
    section: str = Field(min_length=1)
    reason: str
    confidence: float = Field(ge=0.0, le=1.0)


class StaleFinding(BaseModel):
    section: DocSection
    matched_terms: list[str]
    verdict: SectionVerdict


class Insertion(BaseModel):
    item: MissingDoc
    proposal: NewSectionProposal


class DocProposals(BaseModel):
    findings: list[StaleFinding] = Field(default_factory=list)
    insertions: list[Insertion] = Field(default_factory=list)
    covered: list[MissingDoc] = Field(default_factory=list)  # documented by another proposal
    errors: list[str] = Field(default_factory=list)


# User-facing interfaces first, so a later function item can see they're already documented.
MISSING_PRIORITY = {"route": 0, "env_var": 1, "cli_flag": 2, "class": 3, "function": 4}


def normalize_heading(heading: str) -> str:
    return " ".join(heading.replace("`", "").lstrip("#").split()).casefold()


class Analyzer:
    def __init__(self, llm: JsonLLM, workers: int = 4) -> None:
        self._llm = llm
        self._workers = workers
        self._system = render_prompt("system")

    def propose(self, result: AnalysisResult) -> DocProposals:
        proposals = DocProposals()
        changes = describe_changeset(result.changeset)

        with ThreadPoolExecutor(max_workers=self._workers) as pool:
            outcomes = list(pool.map(lambda c: self._review(result, c, changes), result.candidates))
        for outcome in outcomes:
            if isinstance(outcome, StaleFinding):
                proposals.findings.append(outcome)
            else:
                proposals.errors.append(outcome)

        # Sequential, so each new section can see what earlier proposals already document.
        written = [f.verdict.updated_section or "" for f in proposals.findings if f.verdict.stale]
        for item in sorted(result.missing, key=lambda m: MISSING_PRIORITY.get(m.kind, len(MISSING_PRIORITY))):
            if any(is_mentioned(text, item) for text in written):
                proposals.covered.append(item)
                continue
            try:
                insertion = self._draft_missing(result, item, changes)
            except LLMError as exc:
                proposals.errors.append(f"{item.kind} {item.name}: {exc}")
                continue
            proposals.insertions.append(insertion)
            written.append(insertion.proposal.section)
        return proposals

    def _review(self, result: AnalysisResult, candidate: Candidate, changes: str) -> StaleFinding | str:
        section = candidate.section
        prompt = render_prompt(
            "stale_section",
            pr_title=result.pr.title,
            changes=changes,
            hunks=relevant_hunks(result.pr.files, candidate.matched_terms),
            location=section.location,
            heading_path=" > ".join(section.heading_path) or section.heading,
            section=section.text,
        )
        try:
            verdict = self._llm.complete(self._system, prompt, SectionVerdict)
        except LLMError as exc:
            return f"{section.location}: {exc}"
        if verdict.stale and _same_text(verdict.updated_section or "", section.text):
            verdict = verdict.model_copy(update={"stale": False, "updated_section": None})
        return StaleFinding(section=section, matched_terms=candidate.matched_terms, verdict=verdict)

    def _draft_missing(self, result: AnalysisResult, item: MissingDoc, changes: str) -> Insertion:
        # Only the defining file's hunks: a service function must not be documented as the route
        # that wraps it.
        own_files = [f for f in result.pr.files if f.path == item.file] or result.pr.files
        hunks = relevant_hunks(own_files, [item.name])
        anchors = file_anchors(next((f for f in own_files if f.path == item.file), None))
        example = best_example(result.sections, hunks, anchors - {item.name})
        prompt = render_prompt(
            "missing_doc",
            kind=item.kind.replace("_", " "),
            name=item.name,
            file=item.file,
            pr_title=result.pr.title,
            changes=changes,
            hunks=hunks,
            outline=doc_outline(result.sections),
            example_location=example.location if example else "none",
            example=example.text if example else "(none)",
        )

        def check(proposal: NewSectionProposal) -> None:
            _check_placement(proposal, result.sections)

        proposal = self._llm.complete(self._system, prompt, NewSectionProposal, check)
        return Insertion(item=item, proposal=proposal)


def _check_placement(proposal: NewSectionProposal, sections: list[DocSection]) -> None:
    files = sorted({s.path for s in sections})
    if proposal.file not in files:
        raise ValueError(f"file {proposal.file!r} is not one of {files}")
    if proposal.after_heading is None:
        return
    headings = [s.heading for s in sections if s.path == proposal.file]
    wanted = normalize_heading(proposal.after_heading)
    if not any(normalize_heading(h) == wanted for h in headings):
        raise ValueError(
            f"after_heading {proposal.after_heading!r} is not a heading in {proposal.file}; "
            f"choose one of {headings} or null"
        )


def _same_text(a: str, b: str) -> bool:
    return a.strip() == b.strip()
