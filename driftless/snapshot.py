"""Download a repo snapshot (docs, source and runtime files) at a ref as a single tarball.

The archive is read in memory and never extracted: only regular files are kept (no symlinks,
hardlinks or devices), under plain relative paths, within per-file, total-size, member-count and
download-size limits, so a hostile repo can't exhaust a small host.
"""

import io
import tarfile
from pathlib import PurePosixPath
from urllib.parse import quote

import httpx

from driftless.diff_parser import is_source_path
from driftless.doc_index import is_doc_path
from driftless.trust import valid_ref, valid_repo_name

MAX_FILE_BYTES = 512_000
MAX_DOWNLOAD_BYTES = 100_000_000  # compressed tarball
MAX_MEMBERS = 50_000
MAX_TOTAL_BYTES = 40_000_000  # text kept in memory
RUNTIME_SUFFIXES = frozenset({".toml", ".cfg", ".ini", ".json", ".yaml", ".yml", ".html", ".jinja", ".j2", ".sql", ".csv"})
API_URL = "https://api.github.com"


class SnapshotError(RuntimeError):
    """The snapshot is too large, or the repo/ref isn't a valid name."""


def is_runtime_path(path: str) -> bool:
    """Non-source files an app needs to start (requirements, config, data, templates)."""
    p = PurePosixPath(path)
    return (p.name.startswith("requirements") and p.suffix == ".txt") or p.suffix.lower() in RUNTIME_SUFFIXES


def download_snapshot(repo_name: str, ref: str, token: str | None) -> dict[str, str]:
    """Return {path: text} for every doc and source file at `ref` (one HTTP request)."""
    if not valid_repo_name(repo_name) or not valid_ref(ref):
        raise SnapshotError(f"not a valid repository/ref: {repo_name!r} @ {ref!r}")
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    url = f"{API_URL}/repos/{repo_name}/tarball/{quote(ref, safe='/')}"
    buffer = io.BytesIO()
    # Redirects go to codeload.github.com; httpx drops the Authorization header on a host change.
    with httpx.stream("GET", url, headers=headers, follow_redirects=True, timeout=120) as response:
        response.raise_for_status()
        for chunk in response.iter_bytes():
            buffer.write(chunk)
            if buffer.tell() > MAX_DOWNLOAD_BYTES:
                raise SnapshotError(f"the repository tarball is larger than {MAX_DOWNLOAD_BYTES // 1_000_000} MB")
    return read_tarball(buffer.getvalue())


def read_tarball(data: bytes) -> dict[str, str]:
    """Read wanted text files from an in-memory GitHub tarball (never extracted to disk)."""
    files: dict[str, str] = {}
    total = 0
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        for count, member in enumerate(archive, start=1):
            if count > MAX_MEMBERS:
                raise SnapshotError(f"the repository has more than {MAX_MEMBERS} files")
            if not member.isfile() or member.size > MAX_FILE_BYTES:
                continue
            _, _, path = member.name.partition("/")  # drop the "owner-repo-sha/" prefix
            if not _plain_path(path) or not (is_doc_path(path) or is_source_path(path) or is_runtime_path(path)):
                continue
            total += member.size
            if total > MAX_TOTAL_BYTES:
                raise SnapshotError(f"the repository's docs and sources exceed {MAX_TOTAL_BYTES // 1_000_000} MB")
            handle = archive.extractfile(member)
            if handle is None:
                continue
            try:
                files[path] = handle.read(MAX_FILE_BYTES + 1).decode("utf-8")
            except UnicodeDecodeError:
                continue
    return files


def _plain_path(path: str) -> bool:
    p = PurePosixPath(path)
    return bool(path) and not p.is_absolute() and ".." not in p.parts and "\\" not in path and ":" not in path
