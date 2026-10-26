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
- `docdrift/analyzer.py` — LLM: decide which sections are stale, propose rewrites
- `docdrift/verifier.py` — check rewrites against actual code
- `docdrift/pr_writer.py` — build branch/commit/PR with per-change explanations
- `docdrift/memory.py` — SQLite memory of decisions & reviewer feedback
- `docdrift/pipeline.py` — orchestrates the end-to-end run
- `docdrift/server.py` — FastAPI webhook endpoint (HMAC-verified)
- `docdrift/cli.py` — typer CLI (`docdrift run --repo owner/name --pr N`)
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
