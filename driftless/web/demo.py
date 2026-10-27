"""Public demo mode (`PUBLIC_DEMO=true`): anyone may browse and run read-only jobs (Run check,
Analyze); everything that writes to GitHub needs an admin session.

An admin logs in with `ADMIN_TOKEN` (constant-time compare) and gets a signed, HttpOnly, Secure,
SameSite=Lax cookie: `<expiry>.<HMAC of the expiry>`, keyed by a hash of the token, so changing
the token logs every session out and there is no other secret to manage. Job starts are capped
(running jobs per repo and in total) and rate-limited per client IP; failed logins too.
"""

import hashlib
import hmac
import math
import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable
from typing import Any

from fastapi import Request

from driftless.config import Settings
from driftless.jobs import Admit, Job, JobRefused

ADMIN_COOKIE = "driftless_admin"
SESSION_TTL_S = 12 * 3600
MAX_TRACKED_KEYS = 10_000
ADMIN_ONLY = "Admin only on the public demo"


class AdminRequired(Exception):
    """A write to GitHub was requested on the public demo without an admin session."""


class AdminSessions:
    def __init__(self, admin_token: str, ttl_s: int = SESSION_TTL_S, clock: Callable[[], float] = time.time) -> None:
        self._token = admin_token
        self._key = hashlib.sha256(b"driftless-admin-session\0" + admin_token.encode()).digest()
        self._ttl_s = ttl_s
        self._clock = clock

    @property
    def enabled(self) -> bool:
        return bool(self._token)

    @property
    def ttl_s(self) -> int:
        return self._ttl_s

    def check_token(self, token: str) -> bool:
        return self.enabled and hmac.compare_digest(token.encode(), self._token.encode())

    def issue(self) -> str:
        expires = str(int(self._clock()) + self._ttl_s)
        return f"{expires}.{self._sign(expires)}"

    def valid(self, cookie: str | None) -> bool:
        if not self.enabled or not cookie:
            return False
        expires, _, signature = cookie.partition(".")
        if not expires.isdigit() or not hmac.compare_digest(signature, self._sign(expires)):
            return False
        return int(expires) > self._clock()

    def _sign(self, expires: str) -> str:
        return hmac.new(self._key, f"admin:{expires}".encode(), hashlib.sha256).hexdigest()


class RateLimiter:
    """At most `limit` hits per key in a sliding window of `window_s` seconds."""

    def __init__(self, limit: int, window_s: float, clock: Callable[[], float] = time.monotonic) -> None:
        self.limit = limit
        self.window_s = window_s
        self._clock = clock
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def retry_after(self, key: str) -> float:
        """Seconds until `key` may hit again; 0 if it may now."""
        with self._lock:
            hits = self._prune(key)
            if len(hits) < self.limit:
                return 0.0
            return max(0.0, hits[0] + self.window_s - self._clock())

    def hit(self, key: str) -> None:
        with self._lock:
            if len(self._hits) > MAX_TRACKED_KEYS:  # keys are otherwise only dropped when seen again
                for stale in list(self._hits):
                    self._prune(stale)
            self._prune(key)
            self._hits[key].append(self._clock())

    def _prune(self, key: str) -> deque[float]:
        hits = self._hits.get(key, deque())
        cutoff = self._clock() - self.window_s
        while hits and hits[0] <= cutoff:
            hits.popleft()
        if not hits:
            self._hits.pop(key, None)  # the table only holds keys seen in the window
        return hits


class DemoGuard:
    """What the public demo allows one request to do."""

    def __init__(self, settings: Settings) -> None:
        self.public = settings.public_demo
        self.sessions = AdminSessions(settings.admin_token)
        self.starts = RateLimiter(settings.demo_rate_limit, settings.demo_rate_window_s)
        self.logins = RateLimiter(5, 600)  # failed admin logins per IP
        self.all_logins = RateLimiter(30, 600)  # failed admin logins from everyone (rotating IPs)
        self._max_jobs = settings.demo_max_jobs
        self._max_per_repo = settings.demo_max_jobs_per_repo
        self._ip_header = settings.client_ip_header

    def is_admin(self, request: Request) -> bool:
        return self.sessions.valid(request.cookies.get(ADMIN_COOKIE))

    def can_write(self, request: Request) -> bool:
        return not self.public or self.is_admin(request)

    def require_write(self, request: Request) -> None:
        if not self.can_write(request):
            raise AdminRequired()

    def context(self, request: Request) -> dict[str, Any]:
        """For templates: `demo.public`, `demo.admin`, `demo.can_write`."""
        admin = self.public and self.is_admin(request)
        return {"public": self.public, "admin": admin, "can_write": not self.public or admin, "admin_only": ADMIN_ONLY}

    def admit(self, request: Request, repo: str) -> Admit | None:
        """The job-start check for this request (None off the demo): caps for everyone, the
        per-IP rate limit for visitors. A start counts only once the job is created."""
        if not self.public:
            return None
        ip = client_ip(request, self._ip_header)
        limited = not self.is_admin(request)

        def admit(active: list[Job]) -> None:
            same_repo = sum(job.repo.lower() == repo.lower() for job in active)
            if same_repo >= self._max_per_repo:
                raise JobRefused(
                    f"Another run for `{repo}` is still going: the public demo runs one per repo at a time. "
                    "Wait for it to finish (see Activity), then try again."
                )
            if len(active) >= self._max_jobs:
                raise JobRefused(
                    f"The public demo is already running {len(active)} jobs, its limit. Try again in a minute or two."
                )
            if limited:
                wait = self.starts.retry_after(ip)
                if wait:
                    raise JobRefused(
                        f"You've started {self.starts.limit} runs in the last {_minutes(self.starts.window_s)}, "
                        f"the public demo's limit. Try again in {_minutes(wait)}."
                    )
                self.starts.hit(ip)

        return admit


def client_ip(request: Request, header: str = "") -> str:
    """The last entry of the proxy's header (the one the proxy itself added, which a client can't
    forge), else the socket peer."""
    if header:
        forwarded = [part.strip() for part in request.headers.get(header, "").split(",") if part.strip()]
        if forwarded:
            return forwarded[-1]
    return request.client.host if request.client else "unknown"


def _minutes(seconds: float) -> str:
    minutes = max(1, math.ceil(seconds / 60))
    return f"{minutes} minute{'s' if minutes != 1 else ''}"
