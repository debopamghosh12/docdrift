# DocDrift

> An always-on agent that keeps your documentation in sync with your code.

## Problem

Docs rot silently. Every merged PR can rename a flag, change a default, or remove an endpoint, and the README keeps describing the old behavior until a confused user files an issue. Reviewers rarely check docs, and nobody wants to audit every Markdown file after every merge.

## How it works

_Placeholder — to be filled in as the pipeline lands._

1. A PR is merged on a watched repo → GitHub webhook hits DocDrift.
2. DocDrift reads the diff and indexes `README.md` and `docs/**/*.md` into sections.
3. An LLM identifies sections the change made stale and rewrites only those.
4. A verifier checks each rewrite against the actual code.
5. DocDrift opens a follow-up PR explaining every change, and remembers reviewer feedback for next time.

## Setup

Requires Python 3.11.

```bash
# create and activate the virtualenv (Git Bash on Windows)
py -3.11 -m venv .venv
source .venv/Scripts/activate

# install
pip install -e ".[dev]"

# configure
cp .env.example .env   # then fill in GITHUB_TOKEN, WEBHOOK_SECRET, LLM_API_KEY

# check
python -m docdrift.cli --help
pytest
```

## Usage

Analyze a merged PR (no webhook needed):

```bash
docdrift run --repo fastapi/typer --pr 1299 --top 10
```

This prints the PR's ChangeSet (changed symbols, env vars, routes, CLI flags), the doc sections most likely to be stale, and new interfaces that no doc mentions.

## Decision log

- **2026-10-26 — OpenAI-compatible LLM client (Groq, `openai/gpt-oss-120b`).** Using the `openai` SDK with a configurable base URL keeps us provider-agnostic; Groq gives fast inference. Because it's a reasoning model, all structured calls use `response_format={"type": "json_object"}` and are validated with pydantic.
- **2026-10-26 — SQLite via sqlmodel for memory.** Zero-ops persistence that's enough for decisions and reviewer feedback, with pydantic-style models shared with the rest of the code.
- **2026-10-26 — Config via pydantic-settings + `.env`.** One typed `Settings` object; secrets stay out of the repo (`.env` is gitignored, `.env.example` is committed).
- **2026-10-26 — Diff "before" = merge base, "after" = PR head.** GitHub's PR patches are three-dot diffs against the merge base, so comparing merge-base vs. head content matches the patches exactly; diffing against the merge commit would pull in unrelated commits from the base branch. Docs are read at the merge commit (what's live now).
- **2026-10-26 — Full-file AST first, patch reconstruction as fallback.** Python symbols come from `ast` on full before/after content (exact signatures, body-equality rename detection, pydantic `BaseSettings` fields as env vars). If content is unavailable or unparseable we rebuild partial text from the patch and use regexes (also the only path for JS/TS).
- **2026-10-26 — Heuristic candidate scoring before any LLM call.** Sections are scored by weighted mentions of changed terms (removed/renamed > signature change > modified > added; env vars/routes/flags boosted; code-span mentions boosted; plain lowercase words like `run` only count inside code). It's cheap, deterministic, explainable, and keeps the LLM prompt small.
- **2026-10-26 — Test files are excluded from the ChangeSet.** Test helpers and fixtures are never user-documented, and including them flooded the candidate list with noise.
- **2026-10-26 — Parallel doc blob fetches.** Fetching ~150 doc files serially took ~64s on `fastapi/typer`; an 8-thread pool cut a full run to ~21s without adding an async rewrite.
