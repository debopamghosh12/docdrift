"""Precise Python symbol extraction using the `ast` module."""

import ast

from docdrift.diff_parser.symbols import SymbolDef
from docdrift.models import SymbolKind

_FUNCTION_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef)
FunctionNode = ast.FunctionDef | ast.AsyncFunctionDef


def parse_python(source: str) -> ast.Module | None:
    try:
        return ast.parse(source)
    except (SyntaxError, ValueError):
        return None


def extract_python_symbols(tree: ast.Module) -> dict[str, SymbolDef]:
    """Top-level functions and classes, plus methods as `Class.method`."""
    symbols: dict[str, SymbolDef] = {}
    for node in tree.body:
        if isinstance(node, _FUNCTION_TYPES):
            _add(symbols, _function_def(node, node.name, SymbolKind.FUNCTION))
        elif isinstance(node, ast.ClassDef):
            _add(symbols, _class_def(node))
            for child in node.body:
                if isinstance(child, _FUNCTION_TYPES):
                    qualname = f"{node.name}.{child.name}"
                    _add(symbols, _function_def(child, qualname, SymbolKind.METHOD))
    return symbols


def extract_settings_env_vars(tree: ast.Module) -> set[str]:
    """Env vars implied by pydantic-settings `BaseSettings` fields (FIELD -> PREFIX + FIELD)."""
    env_vars: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        if not any(ast.unparse(base).endswith("BaseSettings") for base in node.bases):
            continue
        prefix = _env_prefix(node)
        for stmt in node.body:
            if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                name = stmt.target.id
                if not name.startswith("_") and name != "model_config":
                    env_vars.add(f"{prefix}{name}".upper())
    return env_vars


def _add(symbols: dict[str, SymbolDef], symbol: SymbolDef) -> None:
    symbols[symbol.name] = symbol


def _function_def(node: FunctionNode, qualname: str, kind: SymbolKind) -> SymbolDef:
    prefix = "async " if isinstance(node, ast.AsyncFunctionDef) else ""
    signature = f"{prefix}{node.name}({ast.unparse(node.args)})"
    if node.returns is not None:
        signature += f" -> {ast.unparse(node.returns)}"
    body_key = ast.dump(node.args) + "|" + "".join(ast.dump(stmt) for stmt in node.body)
    return SymbolDef(qualname, kind, signature, body_key)


def _class_def(node: ast.ClassDef) -> SymbolDef:
    bases = [ast.unparse(b) for b in node.bases] + [ast.unparse(k) for k in node.keywords]
    signature = f"class {node.name}({', '.join(bases)})" if bases else f"class {node.name}"
    # Methods are tracked separately; the class body key covers fields/attributes only.
    body_key = "".join(ast.dump(s) for s in node.body if not isinstance(s, _FUNCTION_TYPES))
    return SymbolDef(node.name, SymbolKind.CLASS, signature, body_key)


def _env_prefix(node: ast.ClassDef) -> str:
    for stmt in node.body:
        targets = stmt.targets if isinstance(stmt, ast.Assign) else [getattr(stmt, "target", None)]
        if not any(isinstance(t, ast.Name) and t.id == "model_config" for t in targets):
            continue
        value = getattr(stmt, "value", None)
        if isinstance(value, ast.Call):
            for keyword in value.keywords:
                if keyword.arg == "env_prefix" and isinstance(keyword.value, ast.Constant):
                    return str(keyword.value.value)
    return ""
