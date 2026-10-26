"""Low-level unified diff helpers."""

import re
from dataclasses import dataclass, field

from docdrift.models import ChangedFile, FileStatus

_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@ ?(.*)$")
_DIFF_GIT_RE = re.compile(r"^diff --git a/(.+?) b/(.+)$")


def reconstruct(patch: str) -> tuple[str, str]:
    """Rebuild partial before/after text from a patch (context + removed / context + added)."""
    before: list[str] = []
    after: list[str] = []
    for line in patch.splitlines():
        if _HUNK_RE.match(line):
            before.append("")
            after.append("")
            continue
        if line.startswith("\\"):
            continue
        tag, text = line[:1], line[1:]
        if tag == "-":
            before.append(text)
        elif tag == "+":
            after.append(text)
        else:
            before.append(text)
            after.append(text)
    return "\n".join(before), "\n".join(after)


def hunk_headers(patch: str) -> str:
    """Return the enclosing-scope context git prints after each `@@ ... @@` marker."""
    headers = (m.group(1) for line in patch.splitlines() if (m := _HUNK_RE.match(line)))
    return "\n".join(h for h in headers if h)


@dataclass
class _PendingFile:
    old_path: str
    new_path: str
    status: FileStatus = FileStatus.MODIFIED
    lines: list[str] = field(default_factory=list)
    in_hunks: bool = False

    def finish(self) -> ChangedFile:
        return ChangedFile(
            path=self.new_path,
            status=self.status,
            previous_path=self.old_path if self.status == FileStatus.RENAMED else None,
            patch="\n".join(self.lines),
        )


def parse_unified_diff(text: str) -> list[ChangedFile]:
    """Split `git diff` output into per-file ChangedFile objects (patch only, no full content)."""
    files: list[ChangedFile] = []
    current: _PendingFile | None = None
    for line in text.splitlines():
        if m := _DIFF_GIT_RE.match(line):
            if current:
                files.append(current.finish())
            current = _PendingFile(old_path=m.group(1), new_path=m.group(2))
            continue
        if current is None:
            continue
        if current.in_hunks or line.startswith("@@"):
            current.in_hunks = True
            current.lines.append(line)
        else:
            _apply_header(current, line)
    if current:
        files.append(current.finish())
    return files


def _apply_header(pending: _PendingFile, line: str) -> None:
    if line.startswith("new file mode"):
        pending.status = FileStatus.ADDED
    elif line.startswith("deleted file mode"):
        pending.status = FileStatus.REMOVED
    elif line.startswith("rename from "):
        pending.old_path = line.removeprefix("rename from ")
        pending.status = FileStatus.RENAMED
    elif line.startswith("rename to "):
        pending.new_path = line.removeprefix("rename to ")
        pending.status = FileStatus.RENAMED
