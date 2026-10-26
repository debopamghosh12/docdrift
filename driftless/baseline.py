"""Pre-existing drift: what was already wrong at the PR's base commit, so the PR isn't blamed for it.

- Examples: the docs at the base are run against the app at the base. A head example that fails
  (or had to be fixed) is pre-existing when the same example (file, label, n-th occurrence,
  documented status) already failed there.
- Sections, where it can be decided deterministically: the section is unchanged since the base,
  and every verifier problem in it that names something this PR touched (a matched term) was
  already a problem against the base code. Staleness only the LLM saw, or a new problem, stays
  the PR's.
"""

from collections import Counter

from driftless.code_index import CodeIndex
from driftless.doc_index import index_documents
from driftless.execution import ExampleOutcome, ExampleStatus, ExecutionReport
from driftless.models import DocFile, DocSection
from driftless.proposals import DocProposals, StaleFinding
from driftless.verifier import check_routes, check_signatures, check_symbols

ExampleKey = tuple[str, str, int, int | None]  # file, label, occurrence, documented status


def needs_baseline(proposals: DocProposals) -> tuple[bool, bool]:
    """(examples, sections): whether there is anything a base run could classify."""
    execution = proposals.execution
    examples = bool(execution and (execution.failing or execution.fixed))
    sections = any(f.verification is not None for f in proposals.findings)
    return examples, sections


def mark_pre_existing(
    proposals: DocProposals,
    head_code: CodeIndex,
    base_docs: list[DocFile],
    base_code: CodeIndex | None,
    base_execution: ExecutionReport | None,
) -> None:
    if proposals.execution and base_execution:
        failed_at_base = {
            key for key, o in _keyed(base_execution.outcomes) if o.status != ExampleStatus.PASSED
        }
        for key, outcome in _keyed(proposals.execution.outcomes):
            if outcome.status != ExampleStatus.PASSED and key in failed_at_base:
                outcome.pre_existing = True
    if base_code is not None:
        base_sections = {(s.path, tuple(s.heading_path)): s for s in index_documents(base_docs)}
        for finding in proposals.findings:
            if finding.verification is not None:
                base = base_sections.get((finding.section.path, tuple(finding.section.heading_path)))
                finding.pre_existing = section_stale_at_base(finding, base, head_code, base_code)


def section_stale_at_base(
    finding: StaleFinding, base: DocSection | None, head_code: CodeIndex, base_code: CodeIndex
) -> bool:
    if base is None or base.text != finding.section.text:
        return False  # the PR added or edited this section: its state is the PR's
    terms = finding.matched_terms
    relevant = {p for p in claim_problems(finding.section.text, head_code) if any(t in p for t in terms)}
    return bool(relevant) and relevant <= claim_problems(base.text, base_code)


def claim_problems(text: str, code: CodeIndex) -> set[str]:
    """What the verifier finds wrong with a doc text as it stands (every claim checked, none exempt)."""
    return {*check_signatures(text, code), *check_symbols("", text, code), *check_routes("", text, code)}


def _keyed(outcomes: list[ExampleOutcome]) -> list[tuple[ExampleKey, ExampleOutcome]]:
    seen: Counter[tuple[str, str]] = Counter()
    keyed = []
    for outcome in outcomes:
        seen[(outcome.file, outcome.label)] += 1
        keyed.append(((outcome.file, outcome.label, seen[(outcome.file, outcome.label)], outcome.expected_status), outcome))
    return keyed
