# Driftless

> **Docs that never drift. Driftless runs your docs to prove they work.**
>
> An always-on agent that keeps your documentation in sync with your code.

## Problem

Docs rot silently. Every merged PR can rename a flag, change a default, or remove an endpoint, and the README keeps describing the old behavior until a confused user files an issue. Reviewers rarely check docs, and nobody wants to audit every Markdown file after every merge.

## How it works

1. A PR is merged on a watched repo → GitHub webhook hits Driftless.
2. Driftless reads the diff and indexes `README.md` and `docs/**/*.md` into sections.
3. An LLM identifies sections the change made stale and rewrites only those.
4. A verifier checks each rewrite against the actual code.
5. Driftless starts the app and runs every request example in all the docs, fixing examples until they actually work.
6. Driftless opens a follow-up PR explaining every change, and remembers reviewer feedback for next time.

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
python -m driftless.cli --help
pytest
```

## Usage

Sync the docs with a merged PR (no webhook needed):

```bash
driftless run --repo debopamghosh12/driftless-demo --pr 1            # opens/updates the docs PR
driftless run --repo debopamghosh12/driftless-demo --pr 1 --dry-run  # only print the proposed diff
```

This prints the PR's ChangeSet (changed symbols, env vars, routes, CLI flags), the doc sections most likely to be stale, the LLM's verified rewrites and new sections, and a diff. Without `--dry-run` it then pushes one commit to `driftless/pr-<n>` and opens (or updates) a PR titled `docs: sync docs with #<n> (...)`, printing its URL. If there is nothing to change, it comments on the source PR instead. `GITHUB_TOKEN` needs contents and pull-request write access.

## Executable docs

Driftless doesn't stop at reading the code: it **runs your docs**. Every request example in the docs is executed against the real app, started from the repo at the commit being documented, and an example only counts as correct if it actually works.

- **Examples** are `curl` commands in `bash`/`sh`/`shell`/`console` blocks, or raw requests in `http` blocks (`POST /notes` + headers + JSON body), aimed at `localhost`. Each is paired with its documented result: the first status with a reason phrase that follows it (`200 OK`, `HTTP/1.1 404 Not Found`) and the JSON block after that status. Examples that use shell variables (`$TOKEN`) or other hosts are skipped.
- **The app** is found with `ast` (a module-level `app = FastAPI(...)`, `src/` layouts included) and started with uvicorn on a free port. Its dependencies go into a venv cached under `.driftless_cache/venvs/`, keyed by a hash of `requirements.txt` and the Python version (built with `uv` when available, else `venv` + pip), so only the first run installs anything. Env vars are set to the defaults documented in the docs' configuration tables.
- **Order matters, state doesn't leak.** Each doc file gets a fresh app, and its examples run in document order, so the `POST` that creates note 1 is what the later `GET /notes/1` reads.
- **Comparison.** The status must match. Documented JSON must match the actual JSON in structure: the same keys and the same value types. Ids and timestamps may differ freely. Other value differences (`"Pen"` vs `"Ink"`, 2 items vs 0) are warnings, not failures, since they usually reflect state rather than wrong docs.
- **Fixing.** For each failing example the LLM gets the example, its documented result, the actual request and response, and the route code. It fixes the request or the documented response, and the file is re-run on a fresh app, up to 2 times. The fix is then tidied deterministically: ids and timestamps from the live run are put back to the documented values, and unchanged JSON keeps its original formatting. Each example ends up as **🧪 passed**, **🔁 fixed after running**, or **❌ still failing**. A failing example's doc change is kept, but flagged clearly in the PR.

In `driftless run` and in webhook syncs this happens after the static verifier, on **every** doc file (as the rewrites leave it), even when static analysis finds nothing stale. A PR that only changes runtime behavior, like a status code or a response field, breaks examples without touching any name the docs mention. Any example fixed after running goes into the docs PR, so such a PR still gets one. If examples fail and can't be fixed, and nothing else changed, the source PR gets a comment listing them instead of an "in sync" comment. The docs PR gets a "🧪 Executed examples: X/Y passing" section with a table (file, example, expected vs actual status, result) and lists every example fixed after running. Pass `--no-execute` (or set `EXECUTE_EXAMPLES=false`) to skip it.

### Docs health audit

`driftless check` runs every example in a repo's docs and needs no PR. You can point it at any repo with a FastAPI app:

```bash
driftless check --repo debopamghosh12/driftless-demo    # pass/fail report for main
driftless check --repo owner/name --ref v1.2.0           # any branch, tag or commit
driftless check --repo owner/name --fix                  # fix broken examples, open a PR
```

It exits with status 1 if any example is still failing, so it can gate CI. With `--fix`, the repaired files are committed to `driftless/fix-examples` and a PR is opened (or updated on reruns). As with docs PRs, a file that changed on the default branch since `--ref` is never overwritten.

> **Security note.** Executable docs **run the repository's code on your machine**: its `requirements.txt` is installed and its app is started. Only use it on repositories you trust. The app runs as a normal subprocess, not in a container. It is bound to `127.0.0.1` and gets a copy of your environment with credentials stripped (any variable whose name contains `TOKEN`, `SECRET`, `PASSWORD` or `API_KEY`). It is always killed afterwards, process tree included. Running it in a Docker sandbox with no network or host access is future work.

## Running as a GitHub webhook

Driftless can run always-on: every merged PR on a watched repo triggers the same sync as `driftless run` (analyze, rewrite, verify, open/update the docs PR). For local development, [smee.io](https://smee.io) forwards GitHub's webhook deliveries to your machine (Windows + Git Bash with Node installed).

1. **Set a webhook secret.** Put a random string in `.env` as `WEBHOOK_SECRET` (e.g. from `python -c "import secrets; print(secrets.token_hex(32))"`). `GITHUB_TOKEN` and `LLM_API_KEY` must be set too. Without a secret the server rejects every delivery.
2. **Start the server** (terminal 1):
   ```bash
   source .venv/Scripts/activate
   driftless serve            # http://127.0.0.1:8000
   ```
3. **Create a smee channel.** Open <https://smee.io> and click **Start a new channel**; copy its URL (`https://smee.io/<id>`).
4. **Forward the channel to the server** (terminal 2):
   ```bash
   npx smee-client --url https://smee.io/<id> --target http://localhost:8000/webhook
   ```
5. **Add the webhook on GitHub.** In the watched repo: **Settings → Webhooks → Add webhook**:
   - **Payload URL:** your smee URL (`https://smee.io/<id>`)
   - **Content type:** `application/json`
   - **Secret:** the same value as `WEBHOOK_SECRET`
   - **Which events?** *Let me select individual events* → tick **Pull requests** and **Issue comments** (untick *Pushes*)
   - **Active:** checked → **Add webhook**
6. **Check it.** GitHub sends a `ping` right away; the server log shows `POST /webhook 200`, and the webhook's *Recent Deliveries* tab shows a green tick. Merge a PR and watch the job:
   ```bash
   curl http://localhost:8000/runs     # last 20 jobs: status, timing, docs PR URL or error
   curl http://localhost:8000/health
   ```

How the server behaves:

- `POST /webhook` verifies `X-Hub-Signature-256` (HMAC-SHA256 of the raw body with `WEBHOOK_SECRET`) and returns **401** if it doesn't match.
- `ping` returns `ok`. `pull_request` with `action=closed` and `merged=true` queues a background sync and returns **202** immediately. Unmerged PRs, PRs from `driftless/` branches (Driftless's own, plus `docdrift/` branches from before the rename) and other events (issue comments are reserved for reviewer feedback) are acknowledged with `ignored`.
- A source PR is never synced twice at the same time: a redelivery while its job is queued or running returns `already running` with the existing job.
- Each job's start, finish, docs PR URL or error is logged; `GET /runs` keeps the last 20 jobs in memory (lost on restart).

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
- **2026-10-26 — Every LLM change is verified against the code at the merge commit before it's trusted.** A deterministic verifier (no LLM) checks: (1) signatures in headings/code spans/`def` blocks match the real definition's parameter names, order and defaults — FastAPI `Query(20)` unwraps to `20` and `int(os.getenv("X", "20"))` resolves to `20`, so `limit=NOTES_PAGE_SIZE` vs `20` is judged correctly; (2) new functions/classes/constants/env vars/CLI flags in code exist in the repo (imports and builtins count; names already in the original section are exempt); (3) documented `METHOD /path` and `curl -X` routes exist; (4) fenced Python parses; (5) lines that differ only in whitespace, table padding or typographic look-alikes (e.g. U+2011 non-breaking hyphens) are restored to the original, new table rows are re-padded, and rewrites touching a large share of unrelated lines are flagged. Failures go back to the model once, verbatim; a second failure drops the change. Each change is shown as ✅ verified, 🔁 fixed on retry, or ⚠️ dropped. Why: the previous run silently shipped a wrong default (`fetch_notes(limit=NOTES_PAGE_SIZE)` — that's the route's default, not the service's) and re-padded an unrelated table row; both are now caught deterministically.
- **2026-10-26 — Repo snapshot via one tarball request.** The verifier needs every source file at the merge commit, not just the changed ones. Downloading the tarball (read in memory, never extracted to disk) replaces ~150 per-file blob calls and supersedes the parallel blob fetching above.
- **2026-10-26 — One LLM call per doc file (batched sections), relevant hunks only.** Candidate sections from the same file share one prompt (up to 6 sections / 6k chars) with only the hunks matching their terms. On the demo PR this is 2 review calls instead of 9, and a full verified dry run takes ~2m15s on Groq's free tier instead of ~4m15s.
- **2026-10-26 — Missing items are documented in every file that covers their kind.** Routes/functions: any file mentioning ≥2 siblings (other routes; other names from the same source file). Env vars/flags: a single section must list ≥2 siblings, since they live in tables — otherwise `docs/api.md`'s passing mention of `APP_PORT` would get an env-var section. Siblings removed by the PR count, and if no file qualifies we prefer the file whose rewrite already mentions the item.
- **2026-10-26 — Docs PRs are one commit on the default branch, rebuilt on every run.** `pr_writer` creates the tree and commit with the Git Data API (one commit regardless of file count), then creates or force-moves `driftless/pr-<n>`. An open PR from that branch has its title and body edited, so reruns are idempotent and the PR always reflects the latest verified proposals. A file is only written if its content on the default branch still equals the merge-commit version Driftless edited. Otherwise it is skipped and listed, so someone else's later edit is never silently reverted. With nothing to commit, Driftless posts a comment on the source PR and edits that same comment on reruns (found by a hidden `<!-- driftless:source-pr=N -->` marker). PRs from `driftless/` branches are refused before any analysis, so Driftless never loops on its own PRs.
- **2026-10-26 — Section separators are structure, not content.** Trailing blank lines and `---` breaks are kept from the original section and stripped from the model's text. New sections go after the anchor's real content: before trailing "See ..." lines, blank lines and breaks. They copy the file's `---` style. Why: in a real run the model dropped one `---` and doubled another.
- **2026-10-26 — Invented example values are a verifier failure, not just a prompt rule.** The prompt forbids them, but the model still wrote `NOTES_PAGE_SIZE=30`. A new check fails any `NAME=value` for a repo setting whose value is neither the code's default (`os.getenv("X", "20")` defaults are indexed) nor a value already in the original section. The usual retry then fixes it: live, `NOTES_PAGE_SIZE=10` became `=20` on retry.
- **2026-10-26 — Webhook syncs run as in-process background jobs with an in-memory registry.** `POST /webhook` verifies the HMAC, then hands merged PRs to FastAPI `BackgroundTasks` and returns 202 at once, because a full sync takes minutes on Groq's free tier and GitHub times out deliveries after 10s. `jobs.JobStore` allows one queued/running job per repo+PR (a redelivery gets the existing job instead of a second LLM run), records status/timing/docs PR URL, and keeps the last 20 for `GET /runs`. No queue or worker process: one server and SQLite are enough for a hackathon demo, and syncs are idempotent, so a job lost on restart is fixed by redelivering the webhook. The server and CLI share the same steps (`sync.sync_pull_request` is the headless version); the app is built by a `create_app(settings, sync)` factory so tests inject a fake sync. A missing `WEBHOOK_SECRET` fails closed (every delivery is rejected).
- **2026-10-26 — Renamed DocDrift → Driftless ("Docs that never drift. Driftless runs your docs to prove they work.").** The new name says what the tool promises (docs that never drift), not the problem it fights, and the tagline leads with what sets it apart: every change is checked against the real code before it ships. The package, CLI command, logger names, default SQLite file (`driftless.db`), docs branch prefix (`driftless/pr-<n>`) and hidden comment marker (`<!-- driftless:source-pr=N -->`) all changed. Docs PR bodies now open with a "🤖 Generated by Driftless" banner, so reviewers see at once that a PR is automated. Two compatibility shims keep repos that already ran the tool safe: `docdrift/` branches are still treated as our own (never analyzed, so no loops on old docs PRs), and "in sync" comments with the old marker are edited on rerun instead of duplicated. The GitHub repo URL and the `docdrift-demo` repo keep their names.
- **2026-10-26 — Executable docs: run every example against the real app, fix it until it works.** Static checks prove that names and signatures exist. They can't prove that `curl -X POST ... → 201 Created` is what the API really does. So after the verifier, Driftless starts the app from the snapshot and runs each doc file's `curl`/`http` examples in order, on a fresh app per file. Statuses must match exactly and JSON must match in structure (keys and types), because those are what break a reader's code. Value differences are only warnings: the docs' `{"id": 7}` after a fresh start is state, not a bug. A failing example goes to the LLM with the actual request/response, gets fixed and re-run, up to 2 attempts. We chose a subprocess with uvicorn and a venv cached by requirements hash over Docker because it works on Windows + Git Bash with nothing else installed, and after the first install a check takes a few seconds per file. The trade-off is that it executes repo code locally, which is acceptable for trusted repos and is documented; container isolation is future work. Two things a live run showed: the model wrapped its fix in our `<example>` tags, and it pasted the run's own id and timestamp over the docs' example values. Both are now undone deterministically (`tidy_replacement`), in the same spirit as the verifier's minimal-diff restore. On Windows a venv's `python.exe` is a launcher with a child process, so the server is killed with `taskkill /T` (the whole tree); otherwise the real server would outlive the run.
- **2026-10-26 — Examples run in every doc file on every sync, not just the rewritten ones.** The point of executable docs is to catch drift that static analysis can't see. A PR that changes `POST /items` from 200 to 201 changes no symbol, route path or env var, so static analysis flags nothing and no file would have been "updated" to run. Now every doc file runs, with the rewrites applied, and an example fixed after running is enough to open a docs PR (`final_docs` merges example fixes in any file). The PR body then says static analysis found nothing and shows only the executed-examples section. Examples that still fail with nothing else to commit are listed in the source-PR comment, so Driftless never reports "in sync" while an example is broken. The cost is one app start per doc file with examples per sync, a few seconds once the venv is cached.
