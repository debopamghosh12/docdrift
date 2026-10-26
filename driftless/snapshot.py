"""Download a repo snapshot (docs, source and runtime files) at a ref as a single tarball."""

import io
import tarfile
from pathlib import PurePosixPath

import httpx

from driftless.diff_parser import is_source_path
from driftless.doc_index import is_doc_path

MAX_FILE_BYTES = 512_000
RUNTIME_SUFFIXES = frozenset({".toml", ".cfg", ".ini", ".json", ".yaml", ".yml", ".html", ".jinja", ".j2", ".sql", ".csv"})
API_URL = "https://api.github.com"


def is_runtime_path(path: str) -> bool:
    """Non-source files an app needs to start (requirements, config, data, templates)."""
    p = PurePosixPath(path)
    return (p.name.startswith("requirements") and p.suffix == ".txt") or p.suffix.lower() in RUNTIME_SUFFIXES


def download_snapshot(repo_name: str, ref: str, token: str | None) -> dict[str, str]:
    """Return {path: text} for every doc and source file at `ref` (one HTTP request)."""
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    response = httpx.get(
        f"{API_URL}/repos/{repo_name}/tarball/{ref}",
        headers=headers,
        follow_redirects=True,
        timeout=120,
    )
    response.raise_for_status()
    return read_tarball(response.content)


def read_tarball(data: bytes) -> dict[str, str]:
    """Read wanted text files from an in-memory GitHub tarball (never extracted to disk)."""
    files: dict[str, str] = {}
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        for member in archive:
            if not member.isfile() or member.size > MAX_FILE_BYTES:
                continue
            _, _, path = member.name.partition("/")  # drop the "owner-repo-sha/" prefix
            if not (is_doc_path(path) or is_source_path(path) or is_runtime_path(path)):
                continue
            handle = archive.extractfile(member)
            if handle is None:
                continue
            try:
                files[path] = handle.read().decode("utf-8")
            except UnicodeDecodeError:
                continue
    return files
