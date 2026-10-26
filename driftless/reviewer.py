"""LLM calls: batched section reviews, verification retries, and new-section drafts."""

from driftless.evidence import best_example, doc_outline, file_anchors, relevant_hunks
from driftless.llm import JsonLLM
from driftless.models import Candidate, DocSection, MissingDoc
from driftless.normalize import normalize_heading
from driftless.pipeline import AnalysisResult
from driftless.prompts import render_prompt
from driftless.proposals import BatchVerdicts, NewSectionProposal, SectionVerdict

MAX_SECTIONS_PER_BATCH = 6
MAX_BATCH_SECTION_CHARS = 6000
OUTPUT_TOKENS_PER_SECTION = 700


def batch_candidates(candidates: list[Candidate]) -> list[list[Candidate]]:
    """Group candidates by file (in document order) into batches that fit one prompt."""
    batches: list[list[Candidate]] = []
    by_file: dict[str, list[Candidate]] = {}
    for candidate in candidates:
        by_file.setdefault(candidate.section.path, []).append(candidate)
    for group in by_file.values():
        current: list[Candidate] = []
        size = 0
        for candidate in sorted(group, key=lambda c: c.section.start_line):
            length = len(candidate.section.text)
            if current and (len(current) >= MAX_SECTIONS_PER_BATCH or size + length > MAX_BATCH_SECTION_CHARS):
                batches.append(current)
                current, size = [], 0
            current.append(candidate)
            size += length
        if current:
            batches.append(current)
    return batches


class Reviewer:
    def __init__(self, llm: JsonLLM) -> None:
        self._llm = llm
        self._system = render_prompt("system")

    def review_batch(
        self, result: AnalysisResult, batch: list[Candidate], changes: str
    ) -> list[SectionVerdict]:
        ids = [f"s{i + 1}" for i in range(len(batch))]
        sections = "\n\n".join(
            f'<section id="{id_}" location="{c.section.location}" heading_path="{_heading_path(c.section)}">\n'
            f"{c.section.text}\n</section>"
            for id_, c in zip(ids, batch)
        )
        terms = sorted({t for c in batch for t in c.matched_terms})
        prompt = render_prompt(
            "stale_batch",
            file=batch[0].section.path,
            pr_title=result.pr.title,
            changes=changes,
            hunks=relevant_hunks(result.pr.files, terms),
            sections=sections,
            ids=", ".join(ids),
        )

        def check(reply: BatchVerdicts) -> None:
            got = [s.id for s in reply.sections]
            if sorted(got) != sorted(ids):
                raise ValueError(f"expected exactly one entry for each id {ids}, got {got}")

        output_tokens = 400 + OUTPUT_TOKENS_PER_SECTION * len(batch)
        reply = self._llm.complete(self._system, prompt, BatchVerdicts, check, output_tokens=output_tokens)
        by_id = {s.id: s for s in reply.sections}
        return [SectionVerdict(**by_id[i].model_dump(exclude={"id"})) for i in ids]

    def retry_section(
        self, result: AnalysisResult, candidate: Candidate, changes: str, previous: str, failures: list[str]
    ) -> SectionVerdict:
        section = candidate.section
        prompt = render_prompt(
            "stale_section",
            pr_title=result.pr.title,
            changes=changes,
            hunks=relevant_hunks(result.pr.files, candidate.matched_terms),
            location=section.location,
            heading_path=_heading_path(section),
            section=section.text,
        ) + _feedback(previous, failures)
        return self._llm.complete(self._system, prompt, SectionVerdict)

    def draft_section(
        self,
        result: AnalysisResult,
        item: MissingDoc,
        target_file: str,
        changes: str,
        feedback: tuple[str, list[str]] | None = None,
    ) -> NewSectionProposal:
        # Only the defining file's hunks: a service function must not be documented as the
        # route that wraps it.
        own_files = [f for f in result.pr.files if f.path == item.file] or result.pr.files
        hunks = relevant_hunks(own_files, [item.name])
        file_sections = [s for s in result.sections if s.path == target_file]
        anchors = file_anchors(next((f for f in own_files if f.path == item.file), None))
        example = best_example(file_sections, hunks, anchors - {item.name})
        prompt = render_prompt(
            "missing_doc",
            kind=item.kind.replace("_", " "),
            name=item.name,
            file=item.file,
            target_file=target_file,
            pr_title=result.pr.title,
            changes=changes,
            hunks=hunks,
            outline=doc_outline(file_sections),
            example_location=example.location if example else "none",
            example=example.text if example else "(none)",
        )
        if feedback:
            prompt += _feedback(*feedback)

        def check(proposal: NewSectionProposal) -> None:
            check_placement(proposal, target_file, file_sections)

        return self._llm.complete(self._system, prompt, NewSectionProposal, check)


def check_placement(proposal: NewSectionProposal, target_file: str, sections: list[DocSection]) -> None:
    if proposal.file != target_file:
        raise ValueError(f'file must be "{target_file}", got {proposal.file!r}')
    if proposal.after_heading is None:
        return
    headings = [s.heading for s in sections]
    wanted = normalize_heading(proposal.after_heading)
    if not any(normalize_heading(h) == wanted for h in headings):
        raise ValueError(
            f"after_heading {proposal.after_heading!r} is not a heading in {target_file}; "
            f"choose one of {headings} or null"
        )


def _heading_path(section: DocSection) -> str:
    return " > ".join(section.heading_path) or section.heading


def _feedback(previous: str, failures: list[str]) -> str:
    return render_prompt(
        "verification_feedback",
        previous=previous,
        failures="\n".join(f"- {failure}" for failure in failures),
    )
