# CLAUDE.md — DocDrift

## Context
24-hour hackathon, judged on **build quality, complexity, and functionality**. Optimize for a working, demoable end-to-end flow with clean code.

## What DocDrift is
An always-on agent that keeps documentation in sync with code. When a PR is merged on a watched GitHub repo, it:
1. Reads the merged diff.
2. Finds doc sections (`README.md`, `docs/**/*.md`) that are now stale.
3. Rewrites **only** those sections.
4. Verifies its own output against the actual code.
5. Opens a follow-up PR explaining each change.

It remembers past decisions and reviewer feedback in a SQLite memory.

## Stack
- Python 3.11, FastAPI (webhook server), httpx, PyGithub, pydantic / pydantic-settings, SQLite via sqlmodel, typer (CLI), pytest.
- LLM: `openai` SDK pointed at an OpenAI-compatible endpoint via `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`.
  - We use Groq: `https://api.groq.com/openai/v1`, model `openai/gpt-oss-120b` (a reasoning model).
  - Wherever structured output is needed, request `response_format={"type": "json_object"}` and validate with pydantic.
- Secrets/config from env: `GITHUB_TOKEN`, `WEBHOOK_SECRET`, `LLM_*`. Loaded from `.env` (gitignored); `.env.example` is committed.

## Layout
- `docdrift/config.py` — settings (pydantic-settings, reads `.env`)
- `docdrift/models.py` — shared pydantic models (ChangedFile, PullRequestInfo, ChangeSet, DocSection, Candidate, ...)
- `docdrift/github_client.py` — GitHub API wrapper (PyGithub); before = merge base, after = PR head, docs at merge commit
- `docdrift/diff_parser/` — package: `patch.py` (unified diff parsing), `python_symbols.py` (ast), `regex_symbols.py` (JS/TS + Python fallback), `patterns.py` (env vars, routes, CLI flags), `symbols.py` (before/after comparison); `build_changeset()` in `__init__`
- `docdrift/doc_index.py` — split markdown docs into heading-delimited sections
- `docdrift/candidates.py` — score sections against the ChangeSet; flag missing docs
- `docdrift/report.py` — rich terminal tables
- `docdrift/analyzer.py` — orchestrates: batched reviews -> verify -> one retry with failures -> verified/fixed-on-retry/dropped; missing docs per covering file
- `docdrift/reviewer.py` — LLM calls: batched section review (per file), verification retry, new-section drafts
- `docdrift/proposals.py` — pydantic models for LLM replies, verification status, proposals
- `docdrift/placement.py` — which doc files cover a missing item's kind
- `docdrift/verifier.py` — deterministic checks vs. code at merge commit (signatures, symbols, routes, example values, python blocks, minimal diff)
- `docdrift/code_index.py` — facts from all source files (functions+effective defaults, classes, names, constants, env vars, flags, routes)
- `docdrift/doc_mentions.py` — claims extracted from markdown (signatures, calls, classes, constants, flags, routes, code blocks)
- `docdrift/minimal_diff.py` — restore whitespace/table-padding/look-alike churn; unrelated-change ratio
- `docdrift/snapshot.py` — repo tarball at a ref (docs + sources) in one request
- `docdrift/normalize.py` — heading normalization
- `docdrift/llm.py` — JSON-mode OpenAI-compatible client; pydantic validation; one retry with the error appended
- `docdrift/rate_limit.py` — sliding-window tokens-per-minute limiter (Groq free tier: 8000 TPM)
- `docdrift/evidence.py` — change summary, relevant hunks, doc outline, sibling-anchored example section
- `docdrift/prompts.py` — loads `prompts/*.txt` (`string.Template`, `$placeholders`)
- `docdrift/doc_editor.py` — apply rewrites (line ranges) then insertions (after real content, matching `---` separators); unified diffs
- `docdrift/proposal_report.py` — rich tables for verdicts/insertions; colored diffs
- `docdrift/pr_writer.py` — `DocsRepo` protocol + `PRWriter`: one commit on `docdrift/pr-<n>`, open/update the docs PR idempotently, or comment "in sync" on the source PR
- `docdrift/pr_body.py` — markdown for the docs PR body (summary, table, dropped changes, reasoning) and the sync comment
- `docdrift/github_writer.py` — PyGithub implementation of `DocsRepo` (Git Data API commits, refs, PRs, comments)
- `docdrift/memory.py` — SQLite memory of decisions & reviewer feedback
- `docdrift/pipeline.py` — orchestrates the end-to-end run
- `docdrift/sync.py` — headless `sync_pull_request` (analyze -> propose/verify -> publish), used by the server
- `docdrift/jobs.py` — in-memory job registry: one active job per repo+PR, last 20 kept, logging
- `docdrift/server.py` — FastAPI `create_app()`: `POST /webhook` (HMAC-verified, merged PRs -> background job, 202), `GET /health`, `GET /runs`
- `docdrift/cli.py` — typer CLI (`docdrift run --repo owner/name --pr N [--dry-run] [--min-confidence 0.5]`; without --dry-run opens the docs PR; `docdrift serve` starts the webhook server on :8000)
- `prompts/` — LLM prompt templates
- `tests/` — pytest

## Environment
- Windows + Git Bash. Virtualenv at `.venv`.
- Activate (Git Bash): `source .venv/Scripts/activate`
- Install: `pip install -e ".[dev]"`

## Rules
- Small, readable modules; type hints everywhere; no giant files.
- After each working step: `git commit` with a conventional message (`feat:`, `fix:`, `docs:`, `test:`, `chore:`) and `git push`.
- Maintain `## Decision log` in README.md — append a short entry (what we chose and why) for every real design decision.
- Never hardcode secrets; use `.env` + `.env.example`.
