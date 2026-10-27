# Security

Driftless reads pull requests, asks an LLM to rewrite docs, and **runs the repository's app** to
check the documented examples. It holds a GitHub token, an LLM key, a webhook secret and (on the
public demo) an admin token. This page covers what it protects against, how, and what risk is
left.

## Reporting a vulnerability

Please report security issues privately through GitHub's
[private vulnerability reporting](https://github.com/debopamghosh12/driftless/security/advisories/new)
on this repository, not in a public issue. Include the steps to reproduce and what an attacker
gains. You'll get an answer within a few days. Please give us a reasonable time to fix the issue
before you disclose it.

## Threat model

| Actor | Can do | Must not be able to |
|---|---|---|
| Anonymous visitor of the public demo (`PUBLIC_DEMO=true`) | Browse allow-listed repos; start **Run check** and **Analyze** jobs (rate-limited) | Run code they control; write to GitHub; read secrets; exhaust the host |
| Any GitHub user | Open a PR from a fork, comment on PRs, push to their fork | Make Driftless run their code; make `/driftless apply` commit their content; change memory rules |
| A collaborator with write access | Push branches, run `/driftless apply`, change memory rules | — (trusted: they can already change the code) |
| Prompt injection in PR text, docs or code | Influence what the LLM writes | Change files other than docs; run commands; touch other repos |
| The app under test (trusted code, but possibly buggy or compromised) | Run on the host for a few seconds | Read the server's secrets, database or caches; outlive the run |

## What is protected, and how

### 1. Only trusted code is ever executed (`driftless/trust.py`)

Running examples means installing the repo's requirements and importing its app, which is code
execution. Driftless runs only code that someone with write access put into the repository:

- **Merged PRs:** the merge commit, which is in the base branch's history.
- **Open PRs:** the head runs only if the branch lives in the same repository *and* the PR author
  has write access, checked with the collaborator-permission API. A fork PR is never run, whether a
  visitor clicks **Analyze**, a webhook triggers a pre-merge check, or someone uses the CLI. The
  analysis still happens, but its report says *"Examples were not run: this PR's branch is in a
  fork"*.
- **Audits** (`Run check`, `driftless check`, scheduled Actions): the ref must be a branch or tag of
  the repo, which is resolved to its commit before download, or a commit on the default branch
  (checked with the compare API). Arbitrary SHAs and `refs/pull/N/head` are refused, because
  GitHub serves a fork's commits through the parent repository's API too.
- Every check fails closed: if a lookup errors, the code does not run.

### 2. The sandbox (`driftless/isolation.py`, `driftless/sandbox.py`)

- **Clean environment.** The app gets an allow-list of variables (`PATH`, locale, temp dirs, Windows
  basics) plus the variables its own code requires. It never inherits the host environment, so it
  never sees `GITHUB_TOKEN`, `LLM_API_KEY`, `WEBHOOK_SECRET`, `ADMIN_TOKEN` or cloud credentials.
- **Safe installs.** `requirements.txt` may contain only `package[extras]<version>` lines. Options
  are refused (`-r /proc/1/environ` would echo a file into the error message), and so are URLs,
  VCS references and paths. Packages are installed with `--only-binary :all:`, so no `setup.py` runs.
- **Unprivileged user (Linux, Docker image).** The server runs as root only so that it can start
  every install and app as the `sandbox` user (`SANDBOX_USER`). The probe below confirms that this
  user can't read `/proc/<server>/environ` or `/proc/1/environ` (the secrets), the SQLite job
  database (the server's umask is `077`), or write the cached venvs. Venvs are built as `sandbox`
  with a fresh `HOME` per build (no shared download cache), then made root-owned and read-only,
  so one run can't plant code in a venv that a later run reuses. The lock walks the venv by file
  descriptor and never follows links, so swapping files mid-walk fails the build instead of
  redirecting root's `chown`. Builds of the same requirements are serialized. Processes are
  limited with `prlimit` (256 processes, 1024 open files, 256 MB per file written), and
  `SANDBOX_USER` refuses to run without `prlimit`. Daemons the app double-forks are killed
  whenever no install or app is running, which under constant load can be later than the end of
  their own run.
- **Lifetime.** Each app gets a fresh temp directory and is bound to `127.0.0.1` on a free port. Its
  whole process tree is killed after its examples, and inherited file descriptors are closed.
  Install, startup and request timeouts apply.
- **Documented examples** can only target `localhost`, and redirects are not followed.

We verified this on 2026-10-27 on Linux (WSL Ubuntu, kernel `/proc` rules, server as root,
`SANDBOX_USER=nobody`; the Docker image uses its own `sandbox` user, uid 10002, the same way). A
hostile app ran as uid 65534 and saw no secrets in its environment. It got `PermissionError` on
`/proc/1/environ`, on its parent's `environ` and on the job database, and it could not write the
venv. Its `setsid` daemon was gone after the run, the venv was owned by root, and the build's
`HOME` had been deleted.

### 3. Archives (`driftless/snapshot.py`)

The repo tarball is read **in memory and never extracted**. Only regular files are kept, so
symlinks, hardlinks and devices are skipped, and only under plain relative paths (no absolute
paths, `..`, backslashes or drive letters). The limits are 100 MB downloaded, 50,000 members, 512 KB
per file and 40 MB of kept text. The sandbox writes files only to paths that resolve inside its
temp dir. Repo names and refs are validated before they go into an API URL.

### 4. What gets written to GitHub

- `GitHubWriter.commit_files` refuses any path that isn't `README.md` or `docs/**/*.md`, plain and
  relative, whatever the LLM, a comment or a bug produces. The model can't touch code,
  `.github/workflows`, or files outside the repo.
- `/driftless apply` and memory commands require write permission. Apply only fast-forwards
  same-repo branches.
- The pre-merge comment that `/driftless apply` reads its fixes from is accepted only if a bot or a
  user with write access posted it. A comment from anyone else that pastes the hidden marker
  (pointing at a commit of theirs) is ignored. Stored fix paths must also be doc paths.
- Branch names are fixed (`driftless/pr-<n>`, `driftless/check`), and every write goes to the
  repo being analyzed. The LLM never chooses a repo, branch or command.

### 5. Web app (`driftless/web/`)

- **XSS:** Jinja autoescaping is on everywhere. The only filter that returns markup escapes its
  input first. Diffs, LLM output and repo content are rendered as text.
- **CSP** with a per-request nonce for the one inline script. `frame-ancestors 'none'` and
  `X-Frame-Options: DENY`, `nosniff`, `Referrer-Policy`, HSTS, COOP and `Permissions-Policy` are on
  every response. htmx is pinned with an SRI hash.
- **CSRF:** every POST is refused when `Sec-Fetch-Site` is cross-site or same-site, or when
  `Origin`/`Referer` names another host. The admin cookie is also `SameSite=Lax`.
- **Admin session:** the token is compared in constant time. The cookie is HMAC-signed with an
  expiry and set `HttpOnly`, `Secure` and `SameSite=Lax`. Failed logins are capped per IP (5 per
  10 minutes) and instance-wide (30 per 10 minutes) against rotating IPs.
- **Access:** only `ALLOWED_REPOS` are served, and an empty list means none. On the public demo,
  `/runs` lists only those repos, and every write needs the admin session.
- **No debug surface:** debug is off, unhandled errors give a generic 500, and `/docs`,
  `/redoc` and `/openapi.json` are disabled.
- **Open redirects:** `/open` only redirects to `/r/<validated owner>/<validated name>`.

### 6. Secrets

- Secrets come only from the environment or a gitignored `.env`. `.env.example` holds placeholders.
- Job errors shown on pages and in `/runs` are scrubbed of the GitHub token, webhook secret, LLM key
  and admin token. Setup pages show only whether a secret is set.
- The git history was scanned for `gsk_`, `github_pat_`, `ghp_`/`gho_`/`ghs_`, `sk-`, AWS keys and
  private keys, and for committed `.env`, `.pem` or key files. The only match is the test
  placeholder `gsk_llmsecretkey456`.

### 7. Webhook and abuse limits

- Webhooks need a valid `X-Hub-Signature-256` (constant-time compare), and with no
  `WEBHOOK_SECRET` every delivery is rejected.
- Bodies are capped before they are read into memory: 25 MB for `/webhook` (GitHub's maximum) and
  64 KB for forms.
- On the demo, jobs are capped at 2 running and 1 per repo, and job starts at 5 per IP per 10
  minutes. The client IP is the **last** `X-Forwarded-For` entry. This assumes the host's proxy
  appends the client address as the last hop (true for Render). With a different proxy chain
  (a CDN in front), visitors share one key, which is stricter but never bypassable. Without
  `CLIENT_IP_HEADER`, the socket peer is used.

### 8. GitHub Action

- It uses `pull_request`, never `pull_request_target`, so fork PRs run with no secrets and a
  read-only token. Driftless also skips their pre-merge check explicitly.
- `issue_comment` runs (which do have secrets) only execute code for `/driftless apply`, after the
  write-access and same-repo checks.
- Inputs reach the script through `env:`, never interpolated into shell. `actions/setup-python` is
  pinned to a commit SHA.
- Permissions are the minimum the features need: `contents`, `pull-requests`, `issues` and
  `statuses` write. If you don't use apply or docs PRs, you can reduce them.

### 9. Dependencies

On 2026-10-27, `pip-audit -r <pip freeze of .venv>` (run from a separate venv) reported no known
vulnerabilities. Minimum versions in
`pyproject.toml` exclude releases with known CVEs: jinja2 3.1.6, python-multipart 0.0.18,
starlette 0.47.2 and h11 0.16.

## Residual risks

These are known and accepted for this project's scope:

- **Network egress.** The app under test can reach the internet: it needs PyPI to install, and a
  per-process network namespace isn't available on Render. Trusted code could still exfiltrate
  whatever it can read, but it can't read Driftless's secrets.
- **Without `SANDBOX_USER`** (the CLI on a laptop, a non-root server, macOS or Windows), the app
  runs as your user. The environment is clean, but a malicious app could still read
  `/proc/<ppid>/environ` or your `.env` file. This is why only trusted code is ever run.
- **Venv reuse.** A venv is shared by every repo with the same `requirements.txt`. It is locked
  (root-owned, read-only) once built, but a package downloaded from PyPI isn't checked against
  hashes unless the repo pins them.
- **Trusted code is trusted.** A collaborator with write access, or a compromised dependency on
  PyPI, can run code in the sandbox. The isolation above limits what that code reaches, but it is
  not a VM. For untrusted workloads, run checks in a disposable container or VM.
- **Tailwind from its CDN** can't be pinned with SRI, because `cdn.tailwindcss.com` sends no CORS
  headers. CSP limits scripts to that origin, but a compromise of the CDN would affect the page.
  Pages hold no secrets: the admin cookie is `HttpOnly`.
- **LLM output is untrusted text.** It can only end up in doc files, and those changes go through
  a PR or an explicit `/driftless apply` that a human triggers. A prompt-injected PR can still make
  the proposed docs wrong or misleading, so review docs PRs like any other PR.
- **Quota abuse.** Analyses spend LLM tokens. Per-IP limits and job caps bound the rate, but many
  IPs could still drain a free-tier quota. Rotate the key or set `EXECUTE_EXAMPLES=false` if that
  happens.
- **Rate-limit state is in memory**, so it resets on restart.
