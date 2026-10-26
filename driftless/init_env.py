"""`driftless init`: write the two secrets a new user needs into `.env` in the current folder.

Existing lines are kept: a key that is already there gets its value replaced in place, new keys
are appended. Everything else (LLM endpoint, model, ...) has a default in `config.Settings`.
"""

import re
import subprocess
from pathlib import Path

REQUIRED_SECRETS = ("GITHUB_TOKEN", "LLM_API_KEY")


def merge_env(existing: str, values: dict[str, str]) -> str:
    """`existing` .env text with `values` set: replaced where the key exists, else appended."""
    lines = existing.splitlines()
    remaining = dict(values)
    for i, line in enumerate(lines):
        match = re.match(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        if match and match.group(1) in remaining:
            key = match.group(1)
            lines[i] = f"{key}={remaining.pop(key)}"
    if remaining and lines and lines[-1].strip():
        lines.append("")
    lines += [f"{key}={value}" for key, value in remaining.items()]
    return "\n".join(lines) + "\n"


def write_env(folder: Path, values: dict[str, str]) -> Path:
    path = folder / ".env"
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    path.write_text(merge_env(existing, values), encoding="utf-8")
    return path


def env_is_ignored(folder: Path) -> bool | None:
    """True/False when `folder` is in a git repo (per `git check-ignore`), None when it isn't."""
    try:
        inside = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"], cwd=folder, capture_output=True, text=True
        )
        if inside.returncode != 0:
            return None
        ignored = subprocess.run(["git", "check-ignore", "-q", ".env"], cwd=folder, capture_output=True)
    except OSError:  # git isn't installed
        return None
    return ignored.returncode == 0
