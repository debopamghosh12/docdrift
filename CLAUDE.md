# CLAUDE.md — Driftless

> Docs that never drift. Driftless runs your docs to prove they work.

## Context
24-hour hackathon, judged on **build quality, complexity, and functionality**. Optimize for a working, demoable end-to-end flow with clean code.

## What Driftless is
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
- `driftless/config.py` — settings (pydantic-settings, reads `.env`)
- `driftless/models.py` — shared pydantic models (ChangedFile, PullRequestInfo, ChangeSet, DocSection, Candidate, ...)
- `driftless/github_client.py` — GitHub API wrapper (PyGithub); before = merge base, after = PR head, docs at merge commit
- `driftless/diff_parser/` — package: `patch.py` (unified diff parsing), `python_symbols.py` (ast), `regex_symbols.py` (JS/TS + Python fallback), `patterns.py` (env vars, routes, CLI flags), `symbols.py` (before/after comparison); `build_changeset()` in `__init__`
- `driftless/doc_index.py` — split markdown docs into heading-delimited sections
- `driftless/candidates.py` — score sections against the ChangeSet; flag missing docs
- `driftless/report.py` — rich terminal tables
- `driftless/analyzer.py` — orchestrates: batched reviews -> verify -> one retry with failures -> verified/fixed-on-retry/dropped; missing docs per covering file
- `driftless/reviewer.py` — LLM calls: batched section review (per file), verification retry, new-section drafts
- `driftless/proposals.py` — pydantic models for LLM replies, verification status, proposals
- `driftless/placement.py` — which doc files cover a missing item's kind
- `driftless/verifier.py` — deterministic checks vs. code at merge commit (signatures, symbols, routes, example values, python blocks, minimal diff)
- `driftless/code_index.py` — facts from all source files (functions+effective defaults, classes, names, constants, env vars, flags, routes)
- `driftless/doc_mentions.py` — claims extracted from markdown (signatures, calls, classes, constants, flags, routes, code blocks)
- `driftless/minimal_diff.py` — restore whitespace/table-padding/look-alike churn; unrelated-change ratio
- `driftless/snapshot.py` — repo tarball at a ref (docs + sources) in one request
- `driftless/normalize.py` — heading normalization
- `driftless/llm.py` — JSON-mode OpenAI-compatible client; pydantic validation; one retry with the error appended
- `driftless/rate_limit.py` — sliding-window tokens-per-minute limiter (Groq free tier: 8000 TPM)
- `driftless/evidence.py` — change summary, relevant hunks, doc outline, sibling-anchored example section
- `driftless/prompts/` — package: `__init__` loads its `*.txt` templates via `importlib.resources` (`string.Template`, `$placeholders`); shipped as package data
- `driftless/doc_editor.py` — apply rewrites (line ranges) then insertions (after real content, matching `---` separators); unified diffs
- `driftless/proposal_report.py` — rich tables for verdicts/insertions; colored diffs
- `driftless/pr_writer.py` — `DocsRepo` protocol + `PRWriter`: one commit on `driftless/pr-<n>` (old `docdrift/` branches are still recognized as ours), open/update the docs PR idempotently, or comment "in sync" on the source PR
- `driftless/pr_body.py` — markdown for the docs PR body (summary, table, dropped changes, reasoning) and the sync comment
- `driftless/github_writer.py` — PyGithub implementation of `DocsRepo` (Git Data API commits, refs, PRs, comments)
- `driftless/examples.py` — runnable examples from markdown (curl in shell blocks, `http` blocks) + documented status/JSON; documented env defaults
- `driftless/sandbox.py` — find the FastAPI app (ast), cached venv under `.driftless_cache/`, uvicorn on a free port, kill the process tree
- `driftless/executor.py` — run examples in order against the app; status must match, JSON structure must match (ids/timestamps flexible, value diffs = warnings)
- `driftless/echo.py` — after an accepted fix: request-body values it changed are updated in documented responses (same file, that example on) that echo them, only where the live response confirms; never in requests
- `driftless/example_fixer.py` — LLM fix of a failing example from the actual request/response; deterministic tidy of the fix
- `driftless/app_env.py` — env for the app under test: only vars the code requires without a default (documented value or typed placeholder); vars the code reads are stripped from the host env
- `driftless/baseline.py` — pre-merge: mark examples/sections already stale at the PR's merge base as pre-existing
- `driftless/executable_docs.py` — per doc file: fresh app, run, fix the earliest failing example, re-run (2 attempts each) -> 🧪 passed / 🔁 fixed / ⛓️ dependent on an earlier example / ❌ failing
- `driftless/example_impact.py` — root-cause vs. knock-on example failures; headline wording
- `driftless/execution.py` / `execution_report.py` — outcome models / rich table
- `driftless/memory.py` — SQLite (sqlmodel): job history, per-repo ignore rules, style preferences, docs-PR feedback (accepted/rejected changes); `RepoMemory` (what applies to one run, rule matching)
- `driftless/feedback.py` — docs PR -> remembered changes; memory -> prompt block (`$memory`); the "🧠 Remembered for this repo" line
- `driftless/commands.py` — parse `/driftless <command>` comments; `MemoryCommands` (ignore/unignore/style/unstyle/memory, write access only) and their replies
- `driftless/pipeline.py` — orchestrates the end-to-end run
- `driftless/sync.py` — headless `sync_pull_request` (analyze -> propose/verify -> publish) and `GitHubSyncer` (post-merge / pre-merge / apply / memory jobs; loads the repo's memory into every run, records opened docs PRs for feedback; caches the last pre-merge fixes per PR), used by the server
- `driftless/premerge.py` — `CheckRepo` protocol + `PreMergeChecker`: one edited PR comment, `Driftless / docs-in-sync` commit status, `/driftless apply` (write access, same-repo branches only, fast-forward)
- `driftless/premerge_body.py` — markdown for the pre-merge comment (stale table, collapsed diffs, examples) and apply replies
- `driftless/jobs.py` — job registry: modes (pre-merge / post-merge / apply / memory; web: analyze / check / fix-examples with live stage + in-memory result), one active job per PR and lane (memory: per comment), newer pushes supersede running checks, persisted to SQLite via `JobLog` (unfinished jobs marked interrupted on startup), last 20 kept, logging
- `driftless/server.py` — FastAPI `create_app()`: `POST /webhook` (HMAC-verified, routed by `events.route_event`; merged PRs -> post-merge job, opened/synchronize/reopened/ready_for_review -> pre-merge job, `/driftless apply` comments -> apply job, other `/driftless` commands -> memory job; 202; closed Driftless docs PRs -> accepted/rejected feedback), `GET /health`, `GET /runs` (from SQLite)
- `driftless/progress.py` — `Stage` + `StageProgress` callback: what a long run is doing (shown live by the web app)
- `driftless/audit.py` — docs health audit (`run_audit`, used by `driftless check` and the web): run examples at a ref, optionally fix + open the examples PR
- `driftless/web/` — web app on the same server: `__init__.install_web` (routers, error pages), `access.py` (ALLOWED_REPOS, redaction), `state.py` (WebState, Jinja env/filters), `pages.py` (GET tabs: health/pulls/memory/activity/setup), `actions.py` (POST: check, PR analyze/premerge/apply/docs-pr, memory, webhook; job polling; same-origin check), `github_browser.py` (repo/docs/PRs/hooks reads, cached), `views.py` (reports -> template rows), `job_panel.py` (stages + result per job), `templates/` (Jinja2 + htmx + Tailwind CDN)
- `driftless/events.py` — pure event router (`route_event`): GitHub event + payload -> plan (pre-merge / post-merge / apply / memory / docs-PR feedback / audit / ignored); shared by server and Action
- `driftless/action.py` — `driftless action` (GitHub Action mode): event from GITHUB_EVENT_NAME/PATH, `ActionRunner` runs the plan inline (re-check after apply, fork PRs skipped, fail-on-drift exit code), step outputs + job summary
- `driftless/repo_config.py` — `.driftless.yml` (ignore/style) from the default branch -> in-memory `Memory`; Action-mode replies to memory commands (the exact YAML to commit)
- `driftless/doctor.py` — `driftless doctor` checks (Python, secrets set, LLM answers, token permissions via empty-POST 422-vs-403 probes); never prints secrets
- `driftless/init_env.py` — `driftless init`: merge GITHUB_TOKEN / LLM_API_KEY into ./.env, gitignore check
- `action.yml` — composite GitHub Action (setup-python, pip install the action path, `driftless action`); `examples/driftless-workflow.yml` — copy-ready workflow
- `driftless/cli.py` — typer CLI (`driftless init` / `driftless doctor [--repo]`; `driftless action` in GitHub Actions; `driftless run --repo owner/name --pr N [--dry-run] [--min-confidence 0.5] [--no-execute]`; without --dry-run opens the docs PR; `driftless check --repo owner/name [--ref main] [--fix]` audits every doc example; `driftless serve` starts the webhook server + web app on :8000)
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
