"""Which repos the web app may act on (`ALLOWED_REPOS`), and what it must never show (secrets)."""

import re

from driftless.config import Settings

_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
REDACTED = "[redacted]"


def valid_repo(owner: str, name: str) -> bool:
    return all(_NAME_RE.match(part) and part not in (".", "..") for part in (owner, name))


def parse_repo(text: str) -> tuple[str, str] | None:
    """`owner/name`, also from a pasted GitHub URL; None if it isn't one."""
    text = text.strip().removesuffix(".git").rstrip("/")
    text = re.sub(r"^(https?://)?(www\.)?github\.com/", "", text)
    owner, _, name = text.partition("/")
    return (owner, name) if valid_repo(owner, name) else None


class AllowedRepos:
    """`owner/name` and `owner/*` rules, case-insensitive. No rules: nothing is allowed."""

    def __init__(self, rules: str) -> None:
        self.rules = [r.strip().lower() for r in rules.split(",") if r.strip()]

    def allows(self, repo: str) -> bool:
        repo = repo.lower()
        owner = repo.split("/", 1)[0]
        return any(rule == repo or rule == f"{owner}/*" for rule in self.rules)

    @property
    def exact(self) -> list[str]:
        """The rules naming a single repo (shown as shortcuts on the home page)."""
        return [rule for rule in self.rules if not rule.endswith("/*")]


def redact(text: str | None, settings: Settings) -> str:
    """Remove secret values from text shown in a page (errors can quote a request)."""
    if not text:
        return ""
    for secret in (settings.github_token, settings.webhook_secret, settings.llm_api_key):
        if secret and len(secret) >= 4:
            text = text.replace(secret, REDACTED)
    return text
