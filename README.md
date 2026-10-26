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
- **2026-10-26 — FastAPI routes come from the AST, linked to their handler.** Knowing the handler lets us report a changed decorator (`@app.put` → `@app.patch`) or a changed handler signature as one *modified* route, and report new handlers as their route rather than as an undocumented function. Express routes stay regex-based.
- **2026-10-26 — Route matching is method-aware.** `/notes/{note_id}` documented for GET/DELETE must not hide an undocumented `PATCH /notes/{note_id}`. Path-only mentions still count, at 0.3× weight.
- **2026-10-26 — Fuzzy rename = similar body (≥0.7) AND a shared name token.** `list_notes` → `fetch_notes` (0.83) is a rename; `farewell` vs `welcome` (0.89, no shared token) is not — tiny functions have near-identical ASTs, so similarity alone is unsafe.
- **2026-10-26 — Missing-docs suppression.** Skip route handlers (the route is reported), classes used in a changed handler's signature (request models are documented with the route), and renamed symbols whose old name is documented (that section gets rewritten). Items are deduped by name.
- **2026-10-26 — One LLM call per candidate section, JSON mode + pydantic, one corrective retry.** Every call uses `response_format={"type": "json_object"}`; the reply is validated (`stale`, `confidence`, `reason`, `updated_section`, plus "stale implies updated_section"). On failure we retry once, appending the model's bad reply and the validation error. Groq's 400 `json_validate_failed` is treated the same way.
- **2026-10-26 — Client-side TPM limiter + `reasoning_effort=low`.** Groq's free tier allows 8,000 tokens/min for gpt-oss-120b and each review costs ~2–3k tokens, so 4 parallel workers caused 429 storms that exhausted SDK retries. A sliding-window limiter (settled with real `usage`) paces calls instead; a full 13-call run takes ~4 min on the free tier with zero 429s. Both knobs are env-configurable (`LLM_TPM_LIMIT`, `LLM_REASONING_EFFORT`).
- **2026-10-26 — Missing docs are drafted after section rewrites, sequentially, interfaces first.** Items already mentioned by a rewrite (e.g. `NOTES_PAGE_SIZE` added to the Configuration table) are marked "covered" instead of getting a duplicate section. Routes/env vars go before functions so a function isn't documented as the endpoint wrapping it.
- **2026-10-26 — New-section placement is anchored on same-file siblings.** The example/neighbour section is chosen by mentions of other names from the item's own file (3× weight) plus diff identifiers, and the model only sees that file's hunks. Without this, the service function `update_note` was documented as the `PATCH` endpoint.
- **2026-10-26 — Edits are applied by line range, bottom-up, preserving section separators.** LLMs strip trailing blank lines, so a rewrite keeps the original section's trailing blank-line count; insertions go after the target heading's subsections.
