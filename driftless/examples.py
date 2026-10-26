"""Extract runnable HTTP examples from markdown, each paired with its documented result.

A request is a `curl` command in a bash/sh/shell/console block, or a raw request in an
`http` block (`POST /notes` + headers + body). Its documented result is what follows it in the
same section, up to the next request or heading: the first status code with a reason phrase
("200 OK", "`201 Created`", "HTTP/1.1 404 Not Found") and the first JSON block after it.
"""

import json
import re
import shlex
from dataclasses import dataclass
from http import HTTPStatus
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, Field

SHELL_LANGS = frozenset({"bash", "sh", "shell", "console", "zsh", "terminal"})
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "0.0.0.0", "[::1]"})
METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})

_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})\s*([\w+-]*)")
_HEADING_RE = re.compile(r"^ {0,3}#{1,6}\s")
_STATUS_RE = re.compile(r"(?<![\d.])([1-5]\d{2})\s+[`*]*\s*([A-Z][A-Za-z'-]*(?:\s+[A-Z][A-Za-z'-]*)*)")
_HTTP_REQUEST_RE = re.compile(r"^(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+(\S+)(?:\s+HTTP/[\d.]+)?$")
# curl options that take a value we don't use.
_VALUE_OPTIONS = frozenset({
    "-o", "--output", "-u", "--user", "-A", "--user-agent", "-b", "--cookie", "-e", "--referer",
    "-w", "--write-out", "-m", "--max-time", "--connect-timeout", "-c", "--cookie-jar",
})
_DATA_OPTIONS = frozenset({"-d", "--data", "--data-raw", "--data-binary", "--data-ascii", "--json"})


class Example(BaseModel):
    """One runnable request. Line numbers are 0-based; the span [start, end) covers the request
    block and its documented result, so a fix can replace exactly that text."""

    file: str
    index: int  # order within the file
    method: str
    path: str  # path + query, host stripped
    headers: dict[str, str] = Field(default_factory=dict)
    body: str | None = None
    expected_status: int | None = None
    expected_json: Any = None
    has_expected_json: bool = False
    start_line: int
    end_line: int
    source: str  # the request block as written

    @property
    def label(self) -> str:
        return f"{self.method} {self.path.split('?', 1)[0]}"

    @property
    def line(self) -> int:
        return self.start_line + 1


@dataclass(frozen=True)
class _Block:
    lang: str
    text: str
    start: int  # 0-based line of the opening fence
    end: int  # exclusive, after the closing fence


@dataclass(frozen=True)
class _Request:
    method: str
    path: str
    headers: dict[str, str]
    body: str | None


def extract_examples(path: str, content: str) -> list[Example]:
    lines = content.split("\n")
    blocks = _fenced_blocks(lines)
    by_start = {b.start: b for b in blocks}
    examples: list[Example] = []
    i = 0
    while i < len(lines):
        block = by_start.get(i)
        if block is None:
            i += 1
            continue
        requests = _requests(block)
        if requests:
            # Earlier commands in a multi-command block have no documented result of their own.
            for request in requests[:-1]:
                examples.append(_example(path, len(examples), request, block, block.end, None, None, False))
            status, payload, has_json, end = _documented_result(lines, blocks, block)
            examples.append(_example(path, len(examples), requests[-1], block, end, status, payload, has_json))
        i = block.end
    return examples


def _example(
    path: str, index: int, request: _Request, block: _Block, end: int, status: int | None, payload: Any, has_json: bool
) -> Example:
    return Example(
        file=path,
        index=index,
        method=request.method,
        path=request.path,
        headers=request.headers,
        body=request.body,
        expected_status=status,
        expected_json=payload,
        has_expected_json=has_json,
        start_line=block.start,
        end_line=end,
        source=block.text,
    )


def _fenced_blocks(lines: list[str]) -> list[_Block]:
    blocks: list[_Block] = []
    i = 0
    while i < len(lines):
        match = _FENCE_RE.match(lines[i])
        if not match:
            i += 1
            continue
        fence, lang = match.group(1), match.group(2).lower()
        j = i + 1
        while j < len(lines) and not lines[j].strip().startswith(fence[0] * len(fence)):
            j += 1
        blocks.append(_Block(lang, "\n".join(lines[i + 1 : j]), i, min(j + 1, len(lines))))
        i = j + 1
    return blocks


def _documented_result(
    lines: list[str], blocks: list[_Block], request_block: _Block
) -> tuple[int | None, Any, bool, int]:
    """(status, parsed JSON, whether JSON was documented, exclusive end line of the result)."""
    status: int | None = None
    end = request_block.end
    later = {b.start: b for b in blocks if b.start >= request_block.end}
    i = request_block.end
    while i < len(lines):
        block = later.get(i)
        if block is not None:
            if _requests(block):
                break
            if block.lang == "json" or (block.lang in ("", "text") and _parse_json(block.text)[1]):
                payload, ok = _parse_json(block.text)
                if ok:
                    return status, payload, True, block.end
            if status is None and block.lang == "http":
                status = parse_status(block.text)
                end = block.end if status is not None else end
            i = block.end
            continue
        if _HEADING_RE.match(lines[i]):
            break
        found = parse_status(lines[i])
        if found is not None:
            if status is not None:
                break  # a second status documents another case (e.g. the 404)
            status, end = found, i + 1
        i += 1
    return status, None, False, end


def parse_status(text: str) -> int | None:
    """First 3-digit HTTP status followed by a reason phrase ("201 Created")."""
    for match in _STATUS_RE.finditer(text):
        code = int(match.group(1))
        try:
            HTTPStatus(code)
        except ValueError:
            continue
        return code
    return None


def _parse_json(text: str) -> tuple[Any, bool]:
    stripped = text.strip()
    if not stripped or stripped[0] not in "[{":
        return None, False
    try:
        return json.loads(stripped), True
    except json.JSONDecodeError:
        return None, False


def _requests(block: _Block) -> list[_Request]:
    if block.lang in SHELL_LANGS:
        return [r for r in (parse_curl(c) for c in _shell_commands(block.text)) if r is not None]
    if block.lang == "http":
        request = _parse_http(block.text)
        return [request] if request else []
    return []


def _shell_commands(text: str) -> list[str]:
    """Join backslash continuations; strip `$ ` prompts and comments."""
    commands: list[str] = []
    current = ""
    for raw in text.split("\n"):
        line = raw.strip()
        if not current and line.startswith("$ "):
            line = line[2:]
        if not current and (not line or line.startswith("#")):
            continue
        if line.endswith("\\"):
            current += line[:-1] + " "
            continue
        commands.append(current + line)
        current = ""
    if current:
        commands.append(current)
    return commands


def parse_curl(command: str) -> _Request | None:
    """Parse a curl command against a local server; None for anything else or unrunnable."""
    try:
        tokens = shlex.split(command, comments=True)
    except ValueError:
        return None
    # Ignore anything piped/chained after curl (`| jq`, `&& echo`).
    for stop in ("|", "&&", ";", ">", "||"):
        if stop in tokens:
            tokens = tokens[: tokens.index(stop)]
    if not tokens or tokens[0] != "curl":
        return None
    method: str | None = None
    headers: dict[str, str] = {}
    body: str | None = None
    url: str | None = None
    args = iter(tokens[1:])
    for token in args:
        if token in ("-X", "--request"):
            method = next(args, "GET").upper()
        elif token.startswith("-X") and len(token) > 2:
            method = token[2:].upper()
        elif token in ("-H", "--header"):
            name, _, value = next(args, "").partition(":")
            if name.strip():
                headers[name.strip()] = value.strip()
        elif token in _DATA_OPTIONS:
            body = next(args, "")
            if token == "--json":
                headers.setdefault("Content-Type", "application/json")
        elif token == "-I" or token == "--head":
            method = "HEAD"
        elif token in _VALUE_OPTIONS:
            next(args, None)
        elif not token.startswith("-") and url is None:
            url = token
    if url is None or "$" in url or (body is not None and body.startswith("@")):
        return None
    path = _local_path(url)
    if path is None:
        return None
    return _Request(method or ("POST" if body is not None else "GET"), path, headers, body)


def _local_path(url: str) -> str | None:
    if "://" not in url:
        url = "http://" + url
    parts = urlsplit(url)
    if (parts.hostname or "") not in LOCAL_HOSTS and f"[{parts.hostname}]" not in LOCAL_HOSTS:
        return None
    path = parts.path or "/"
    return f"{path}?{parts.query}" if parts.query else path


def _parse_http(text: str) -> _Request | None:
    lines = text.strip("\n").split("\n")
    match = _HTTP_REQUEST_RE.match(lines[0].strip()) if lines else None
    if not match:
        return None
    method, target = match.group(1), match.group(2)
    if target.startswith("http"):
        path = _local_path(target)
        if path is None:
            return None
    else:
        path = target
    headers: dict[str, str] = {}
    i = 1
    while i < len(lines) and lines[i].strip():
        name, _, value = lines[i].partition(":")
        if name.lower() != "host":
            headers[name.strip()] = value.strip()
        i += 1
    body = "\n".join(lines[i + 1 :]).strip() or None
    if body is not None and not any(k.lower() == "content-type" for k in headers):
        headers["Content-Type"] = "application/json"
    return _Request(method, path, headers, body)


def documented_env_defaults(content: str) -> dict[str, str]:
    """Env var defaults from markdown tables with a "Default" column: | `APP_PORT` | `8000` |."""
    defaults: dict[str, str] = {}
    default_col: int | None = None
    for line in content.split("\n"):
        stripped = line.strip()
        if not stripped.startswith("|"):
            default_col = None
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if default_col is None:
            lowered = [c.lower().strip("*") for c in cells]
            if "default" in lowered:
                default_col = lowered.index("default")
            continue
        if set(stripped) <= set("|-: "):
            continue
        if default_col >= len(cells):
            continue
        name = cells[0].strip("`* ")
        value = cells[default_col].strip()
        if re.fullmatch(r"[A-Z][A-Z0-9_]+", name) and value.startswith("`") and value.endswith("`"):
            defaults[name] = value.strip("`")
    return defaults
