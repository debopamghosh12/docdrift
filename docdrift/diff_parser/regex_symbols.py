"""Regex-based symbol extraction for JS/TS, and a fallback for unparseable Python."""

import re

from docdrift.diff_parser.symbols import SymbolDef
from docdrift.models import SymbolKind

_JS_FUNCTION = re.compile(
    r"^[ \t]*(?:export[ \t]+)?(?:default[ \t]+)?(?:async[ \t]+)?function[ \t]*\*?[ \t]*"
    r"([A-Za-z_$][\w$]*)[ \t]*(?:<[^>\n]*>)?[ \t]*\(([^)]*)\)",
    re.MULTILINE,
)
_JS_ARROW = re.compile(
    r"^[ \t]*(?:export[ \t]+)?(?:const|let|var)[ \t]+([A-Za-z_$][\w$]*)[ \t]*(?::[^=\n]+)?="
    r"[ \t]*(?:async[ \t]+)?(?:\(([^)]*)\)|([A-Za-z_$][\w$]*))[ \t]*(?::[^=\n]+)?=>",
    re.MULTILINE,
)
_JS_CLASS = re.compile(
    r"^[ \t]*(?:export[ \t]+)?(?:default[ \t]+)?(?:abstract[ \t]+)?class[ \t]+"
    r"([A-Za-z_$][\w$]*)([^{\n]*)",
    re.MULTILINE,
)
_PY_DEF = re.compile(
    r"^([ \t]*)(async[ \t]+)?def[ \t]+(\w+)[ \t]*\(([^)]*)\)(?:[ \t]*->[ \t]*([^:\n]+))?",
    re.MULTILINE,
)
_PY_CLASS = re.compile(r"^([ \t]*)class[ \t]+(\w+)([^:\n]*)", re.MULTILINE)


def _norm(text: str) -> str:
    return " ".join(text.split())


def extract_js_symbols(source: str) -> dict[str, SymbolDef]:
    symbols: dict[str, SymbolDef] = {}
    for m in _JS_FUNCTION.finditer(source):
        name = m.group(1)
        symbols[name] = SymbolDef(name, SymbolKind.FUNCTION, f"{name}({_norm(m.group(2))})")
    for m in _JS_ARROW.finditer(source):
        name, params = m.group(1), m.group(2) if m.group(2) is not None else m.group(3)
        symbols[name] = SymbolDef(name, SymbolKind.FUNCTION, f"{name}({_norm(params)})")
    for m in _JS_CLASS.finditer(source):
        name = m.group(1)
        symbols[name] = SymbolDef(name, SymbolKind.CLASS, _norm(f"class {name} {m.group(2)}"))
    return symbols


def extract_python_symbols_regex(source: str) -> dict[str, SymbolDef]:
    """Best-effort Python extraction for code fragments that `ast` can't parse."""
    matches = sorted(
        [(m.start(), "def", m) for m in _PY_DEF.finditer(source)]
        + [(m.start(), "class", m) for m in _PY_CLASS.finditer(source)],
        key=lambda item: item[0],
    )
    symbols: dict[str, SymbolDef] = {}
    current_class: str | None = None
    for _, kind, m in matches:
        indented = bool(m.group(1))
        if kind == "class":
            name = m.group(2)
            if not indented:
                current_class = name
            symbols[name] = SymbolDef(name, SymbolKind.CLASS, _norm(f"class {name}{m.group(3)}"))
            continue
        if not indented:
            current_class = None
        prefix = "async " if m.group(2) else ""
        signature = f"{prefix}{m.group(3)}({_norm(m.group(4))})"
        if m.group(5):
            signature += f" -> {_norm(m.group(5))}"
        if indented:
            qualname = f"{current_class}.{m.group(3)}" if current_class else m.group(3)
            symbols[qualname] = SymbolDef(qualname, SymbolKind.METHOD, signature)
        else:
            symbols[m.group(3)] = SymbolDef(m.group(3), SymbolKind.FUNCTION, signature)
    return symbols
