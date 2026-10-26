"""Orchestrate LLM proposals and verify each one against the code before trusting it.

Flow: batched section reviews -> verify each rewrite -> on failure, one retry with the exact
failure messages -> verified / fixed on retry / dropped. Missing items are then drafted for
every doc file that covers their kind, skipping files where a rewrite already documents them.
"""

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

from driftless.candidates import build_terms, is_mentioned
from driftless.evidence import describe_changeset
from driftless.llm import JsonLLM, LLMError
from driftless.models import Candidate, ChangeSet, MissingDoc
from driftless.pipeline import AnalysisResult
from driftless.placement import covering_files, fallback_file
from driftless.proposals import (
    CoveredItem,
    DocProposals,
    Insertion,
    NewSectionProposal,
    SectionVerdict,
    StaleFinding,
    Verification,
    VerificationStatus,
)
from driftless.reviewer import Reviewer, batch_candidates
from driftless.verifier import Verifier

# User-facing interfaces first, so a later function item can see they're already documented.
MISSING_PRIORITY = {"route": 0, "env_var": 1, "cli_flag": 2, "class": 3, "function": 4}
Retry = Callable[[str, list[str]], str | None]


def changed_names(changeset: ChangeSet) -> list[str]:
    """Names whose mention makes a doc line 'related' to the change (route terms -> their path)."""
    return sorted({term.text.split(" ", 1)[-1] for term in build_terms(changeset)})


class Analyzer:
    def __init__(self, llm: JsonLLM, workers: int = 2) -> None:
        self._reviewer = Reviewer(llm)
        self._workers = workers

    def propose(self, result: AnalysisResult, min_confidence: float = 0.5) -> DocProposals:
        proposals = DocProposals()
        changes = describe_changeset(result.changeset)
        verifier = Verifier(result.code, changed_names(result.changeset))

        batches = batch_candidates(result.candidates)
        with ThreadPoolExecutor(max_workers=self._workers) as pool:
            outcomes = list(pool.map(lambda b: self._review(result, b, changes), batches))
        reviewed: list[tuple[Candidate, SectionVerdict]] = []
        for batch, outcome in zip(batches, outcomes):
            if isinstance(outcome, str):
                proposals.errors.append(outcome)
            else:
                reviewed.extend(zip(batch, outcome))

        def finish(pair: tuple[Candidate, SectionVerdict]) -> StaleFinding:
            return self._verify_finding(result, *pair, changes, verifier, min_confidence)

        with ThreadPoolExecutor(max_workers=self._workers) as pool:
            proposals.findings = list(pool.map(finish, reviewed))
        self._document_missing(result, proposals, changes, verifier, min_confidence)
        return proposals

    def _review(self, result: AnalysisResult, batch: list[Candidate], changes: str) -> list[SectionVerdict] | str:
        try:
            return self._reviewer.review_batch(result, batch, changes)
        except LLMError as exc:
            locations = ", ".join(c.section.location for c in batch)
            return f"{locations}: {exc}"

    def _verify_finding(
        self,
        result: AnalysisResult,
        candidate: Candidate,
        verdict: SectionVerdict,
        changes: str,
        verifier: Verifier,
        min_confidence: float,
    ) -> StaleFinding:
        section = candidate.section
        finding = StaleFinding(section=section, matched_terms=candidate.matched_terms, verdict=verdict)
        if verdict.stale and (verdict.updated_section or "").strip() == section.text.strip():
            finding.verdict = verdict.model_copy(update={"stale": False, "updated_section": None})
        if not finding.verdict.stale or verdict.confidence < min_confidence:
            return finding

        def retry(previous: str, failures: list[str]) -> str | None:
            again = self._reviewer.retry_section(result, candidate, changes, previous, failures)
            return again.updated_section if again.stale else None

        text, finding.verification = verify_with_retry(
            section.text, verdict.updated_section or "", verifier, retry
        )
        finding.verdict = finding.verdict.model_copy(update={"updated_section": text})
        return finding

    def _document_missing(
        self,
        result: AnalysisResult,
        proposals: DocProposals,
        changes: str,
        verifier: Verifier,
        min_confidence: float,
    ) -> None:
        # Sequential, so each new section can see what earlier proposals already document.
        written: dict[str, list[str]] = {doc.path: [] for doc in result.docs}
        for finding in proposals.findings:
            if finding.verification and finding.verification.applied:
                written[finding.section.path].append(finding.verdict.updated_section or "")

        for item in sorted(result.missing, key=lambda m: MISSING_PRIORITY.get(m.kind, len(MISSING_PRIORITY))):
            files = covering_files(
                item, result.docs, result.sections, result.code, result.changeset, result.pr.files
            )
            if not files:
                # No file documents this kind yet: prefer where a rewrite already mentions it.
                mentioned_in = [p for p, texts in written.items() if any(is_mentioned(t, item) for t in texts)]
                fallback = fallback_file(item, result.sections, result.pr.files)
                files = mentioned_in or ([fallback] if fallback else [])
            for path in files:
                if any(is_mentioned(text, item) for text in written.get(path, [])):
                    proposals.covered.append(CoveredItem(item=item, file=path))
                    continue
                insertion = self._draft(result, item, path, changes, verifier, min_confidence, proposals)
                if insertion is None:
                    continue
                proposals.insertions.append(insertion)
                if insertion.verification and insertion.verification.applied:
                    written.setdefault(path, []).append(insertion.proposal.section)

    def _draft(
        self,
        result: AnalysisResult,
        item: MissingDoc,
        path: str,
        changes: str,
        verifier: Verifier,
        min_confidence: float,
        proposals: DocProposals,
    ) -> Insertion | None:
        try:
            proposal = self._reviewer.draft_section(result, item, path, changes)
        except LLMError as exc:
            proposals.errors.append(f"{item.kind} {item.name} in {path}: {exc}")
            return None
        if proposal.confidence < min_confidence:
            return Insertion(item=item, proposal=proposal)

        latest: list[NewSectionProposal] = [proposal]

        def retry(previous: str, failures: list[str]) -> str | None:
            latest.append(self._reviewer.draft_section(result, item, path, changes, (previous, failures)))
            return latest[-1].section

        text, verification = verify_with_retry("", proposal.section, verifier, retry)
        final = latest[-1].model_copy(update={"section": text})
        return Insertion(item=item, proposal=final, verification=verification)


def verify_with_retry(
    original: str, updated: str, verifier: Verifier, retry: Retry
) -> tuple[str, Verification]:
    """Verify; on failure retry once with the exact failure messages; else drop."""
    first = verifier.check(original, updated)
    if first.ok:
        return first.text, Verification(
            status=VerificationStatus.VERIFIED, flags=first.flags, restored_lines=first.restored_lines
        )
    try:
        retried = retry(first.text, first.failures)
    except LLMError as exc:
        return first.text, Verification(
            status=VerificationStatus.DROPPED, failures=first.failures, note=f"retry failed: {exc}"
        )
    if retried is None:
        return first.text, Verification(
            status=VerificationStatus.DROPPED,
            failures=first.failures,
            note="on retry the model withdrew the change",
        )
    second = verifier.check(original, retried)
    if second.ok:
        return second.text, Verification(
            status=VerificationStatus.FIXED_ON_RETRY,
            failures=first.failures,
            flags=second.flags,
            restored_lines=second.restored_lines,
        )
    return second.text, Verification(
        status=VerificationStatus.DROPPED, failures=first.failures, retry_failures=second.failures
    )
