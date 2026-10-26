"""Extract verifiable claims from markdown: signatures, symbols, flags, routes, code blocks."""

import ast
import re
from dataclasses import dataclass, field

PY_LANGS = frozenset({"python", "py", "python3"})
JS_LANGS = frozenset({"js", "javascript", "ts", "typescript", "jsx", "tsx", "mjs"})
SHELL_LANGS = frozenset({"bash", "sh", "shell", "console", "zsh", "powershell", "ps1", "cmd", ""})
# Lines starting with these run third-party tools whose flags aren't ours to verify.
EXTERNAL_COMMANDS = frozenset(
    {"curl", "wget", "pip", "pip3", "uvicorn", "gunicorn", "docker", "git", "npm", "npx", "yarn",
     "pnpm", "http", "source", "cd", "export", "brew", "apt", "apt-get", "poetry", "pytest"}
)
HTTP_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")

_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})[ \t]*([\w+-]*)[^\n]*\n(.*?)^ {0,3}\1[ \t]*$", re.M | re.S)
_SPAN_RE = re.compile(r"`([^`\n]+)`")
_HEADING_RE = re.compile(r"^ {0,3}#{1,6}[ \t]+(.+?)[ \t]*#*[ \t]*$", re.M)
_SIGNATURE_RE = re.compile(
    r"\s*(?:async\s+)?(?:def\s+|function\s+)?([A-Za-z_$][\w.$]*)\s*\((.*)\)\s*(?:->\s*[^`]*|:\s*\w[^`]*)?:?\s*",
    re.S,
)
_CALL_RE = re.compile(r"(?<![\w.$])([A-Za-z_$][\w$]*)\(")
_CLASS_RE = re.compile(r"(?<![\w.])([A-Z][a-z0-9]+(?:[A-Z][A-Za-z0-9]*)+|[A-Z]{2,}[a-z][A-Za-z0-9]*)\b")
_CONSTANT_RE = re.compile(r"(?<![\w-])([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+)\b")
_FLAG_RE = re.compile(r"(?<![\w-])(--[a-z][\w-]*)")
_ROUTE_RE = re.compile(rf"(?<![\w/])({'|'.join(HTTP_METHODS)})\s+(/[^\s`'\"<>)?#,;]*)")
_CURL_RE = re.compile(r"curl\b[^\n]*?-X\s+([A-Z]+)[^\n]*?https?://[^/\s'\"]+(/[^\s'\"?#]*)")
_QUOTED_RE = re.compile(r"\"[^\"\n]*\"|'[^'\n]*'")


@dataclass(frozen=True)
class CodeBlock:
    lang: str
    text: str


@dataclass(frozen=True)
class DocParam:
    name: str
    default: str | None = None


@dataclass(frozen=True)
class DocSignature:
    name: str
    params: tuple[DocParam, ...]
    text: str


@dataclass
class Mentions:
    calls: set[str] = field(default_factory=set)
    classes: set[str] = field(default_factory=set)
    constants: set[str] = field(default_factory=set)
    flags: set[str] = field(default_factory=set)
    routes: set[tuple[str, str]] = field(default_factory=set)
    signatures: list[DocSignature] = field(default_factory=list)
    blocks: list[CodeBlock] = field(default_factory=list)


def code_blocks(markdown: str) -> list[CodeBlock]:
    return [CodeBlock(m.group(2).lower(), m.group(3)) for m in _FENCE_RE.finditer(markdown)]


def extract_mentions(markdown: str) -> Mentions:
    mentions = Mentions(blocks=code_blocks(markdown))
    prose = _FENCE_RE.sub("", markdown)
    spans = _SPAN_RE.findall(prose)

    for span in spans:
        _scan_code(mentions, span, calls=True, classes=True)
        if not _is_external_command(span):
            mentions.flags |= set(_FLAG_RE.findall(span))
    for block in mentions.blocks:
        if block.lang in PY_LANGS | JS_LANGS:
            _scan_code(mentions, block.text, calls=True, classes=True)
        else:
            mentions.constants |= set(_CONSTANT_RE.findall(block.text))
        if block.lang in SHELL_LANGS:
            for line in block.text.splitlines():
                if not _is_external_command(line):
                    mentions.flags |= set(_FLAG_RE.findall(line))
            mentions.routes |= {(m, p) for m, p in _CURL_RE.findall(block.text)}
        if block.lang in PY_LANGS:
            mentions.signatures += _block_signatures(block.text)

    mentions.routes |= {(m, p.rstrip(".:")) for m, p in _ROUTE_RE.findall(markdown)}
    for text in spans + [h.replace("`", "") for h in _HEADING_RE.findall(prose)]:
        signature = parse_signature(text)
        if signature and signature not in mentions.signatures:
            mentions.signatures.append(signature)
    return mentions


def parse_signature(text: str) -> DocSignature | None:
    """Parse `name(a, b=1)` (call or def style). Returns None for usage examples like `f(1)`."""
    m = _SIGNATURE_RE.fullmatch(text)
    if not m:
        return None
    name, args = m.group(1).split(".")[-1], m.group(2)
    try:
        tree = ast.parse(f"def _f({args}): pass")
    except SyntaxError:
        return _call_signature(name, args, text)
    func = tree.body[0]
    assert isinstance(func, ast.FunctionDef)
    return DocSignature(name, _def_params(func.args), text.strip())


def _call_signature(name: str, args: str, text: str) -> DocSignature | None:
    try:
        call = ast.parse(f"_f({args})", mode="eval").body
    except SyntaxError:
        return None
    assert isinstance(call, ast.Call)
    if not all(isinstance(a, ast.Name) for a in call.args) or any(k.arg is None for k in call.keywords):
        return None
    params = [DocParam(a.id) for a in call.args if isinstance(a, ast.Name)]
    params += [DocParam(k.arg, ast.unparse(k.value)) for k in call.keywords if k.arg]
    return DocSignature(name, tuple(params), text.strip())


def _def_params(args: ast.arguments) -> tuple[DocParam, ...]:
    positional = args.posonlyargs + args.args
    defaults: list[ast.expr | None] = [None] * (len(positional) - len(args.defaults)) + list(args.defaults)
    params = [DocParam(a.arg, ast.unparse(d) if d is not None else None) for a, d in zip(positional, defaults)]
    params += [
        DocParam(a.arg, ast.unparse(d) if d is not None else None)
        for a, d in zip(args.kwonlyargs, args.kw_defaults)
    ]
    return tuple(p for p in params if p.name not in ("self", "cls"))


def _block_signatures(text: str) -> list[DocSignature]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    return [
        DocSignature(node.name, _def_params(node.args), f"def {node.name}(...)")
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]


def _scan_code(mentions: Mentions, code: str, calls: bool, classes: bool) -> None:
    unquoted = _QUOTED_RE.sub('""', code)  # names inside string literals aren't code references
    if calls:
        mentions.calls |= set(_CALL_RE.findall(unquoted))
    if classes:
        mentions.classes |= set(_CLASS_RE.findall(unquoted))
    mentions.constants |= set(_CONSTANT_RE.findall(code))


def _is_external_command(line: str) -> bool:
    words = line.strip().lstrip("$> ").split()
    while words and "=" in words[0]:  # skip `VAR=value` prefixes
        words = words[1:]
    return bool(words) and words[0] in EXTERNAL_COMMANDS
