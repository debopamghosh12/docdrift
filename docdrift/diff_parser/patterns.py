"""Regex extractors for env vars, HTTP routes, and CLI flags."""

import re

_ENV_PATTERNS = [
    re.compile(r"""os\.(?:getenv|environ\.get)\(\s*['"]([A-Za-z_][A-Za-z0-9_]*)['"]"""),
    re.compile(r"""os\.environ\[\s*['"]([A-Za-z_][A-Za-z0-9_]*)['"]\s*\]"""),
    re.compile(r"""process\.env\.([A-Za-z_][A-Za-z0-9_]*)"""),
    re.compile(r"""process\.env\[\s*['"`]([A-Za-z_][A-Za-z0-9_]*)['"`]\s*\]"""),
    re.compile(r"""import\.meta\.env\.([A-Za-z_][A-Za-z0-9_]*)"""),
]

# Matches FastAPI decorators (`@app.get("/x")`, `@router.post(path="/x")`) and Express calls
# (`app.get('/x', ...)`, `router.all("/x")`). Requiring a leading "/" filters out dict.get("key").
_ROUTE_RE = re.compile(
    r"""\b[A-Za-z_$][\w$]*\.(get|post|put|patch|delete|head|options|all)\(\s*"""
    r"""(?:path\s*=\s*)?['"`](/[^'"`\s]*)['"`]"""
)

_FLAG_CALL_RE = re.compile(r"(?:\badd_argument|\.option|\.requiredOption|\bOption)\s*\(")
_TYPER_PARAM_RES = [
    re.compile(r"(\w+)\s*:\s*[^=\n]*?=\s*typer\.Option\("),
    re.compile(r"(\w+)\s*:\s*Annotated\[[^\n]*?typer\.Option\("),
]
_STRING_RE = re.compile(r"""(['"`])(.*?)\1""")
_FLAG_TOKEN_RE = re.compile(r"(?<![\w-])(--[A-Za-z][\w-]*)")
_MAX_CALL_CHARS = 1000


def extract_env_vars(text: str) -> set[str]:
    return {m.group(1) for pattern in _ENV_PATTERNS for m in pattern.finditer(text)}


def extract_routes(text: str) -> set[tuple[str, str]]:
    """Return (METHOD, path) pairs."""
    return {(m.group(1).upper(), m.group(2)) for m in _ROUTE_RE.finditer(text)}


def extract_cli_flags(text: str) -> set[str]:
    """Long flags from argparse/click/commander/typer calls, plus typer's implicit param flags."""
    flags: set[str] = set()
    for m in _FLAG_CALL_RE.finditer(text):
        flags |= _flags_in(_call_args(text, m.end()))
    for pattern in _TYPER_PARAM_RES:
        for m in pattern.finditer(text):
            if not _flags_in(_call_args(text, m.end())):
                flags.add("--" + m.group(1).replace("_", "-"))
    return flags


def _flags_in(args: str) -> set[str]:
    """Flags from declaration strings like "--verbose" or "-p, --port <n>" (not help text)."""
    declarations = (s.group(2) for s in _STRING_RE.finditer(args) if s.group(2).startswith("-"))
    return {flag for decl in declarations for flag in _FLAG_TOKEN_RE.findall(decl)}


def _call_args(text: str, start: int) -> str:
    """Return the argument text of a call whose opening paren ends at `start`."""
    depth = 1
    end = min(len(text), start + _MAX_CALL_CHARS)
    for i in range(start, end):
        if text[i] in "([{":
            depth += 1
        elif text[i] in ")]}":
            depth -= 1
            if depth == 0:
                return text[start:i]
    return text[start:end]
