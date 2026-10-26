"""Facts about the code at the merge commit, used to verify generated documentation."""

import ast
import re
from pathlib import PurePosixPath

from pydantic import BaseModel, Field

from driftless.diff_parser import JS_EXTENSIONS, PYTHON_EXTENSIONS
from driftless.diff_parser.patterns import extract_cli_flags, extract_env_vars, extract_routes
from driftless.diff_parser.python_symbols import (
    extract_python_routes,
    extract_settings_env_vars,
    parse_python,
)
from driftless.diff_parser.regex_symbols import extract_js_symbols
from driftless.models import SourceFile, SymbolKind

# FastAPI/pydantic/typer wrappers whose first argument is the parameter's real default.
PARAM_WRAPPERS = frozenset(
    {"Query", "Path", "Body", "Header", "Cookie", "Form", "File", "Field", "Option", "Argument"}
)
_ENV_GETTERS = frozenset({"os.getenv", "os.environ.get", "getenv", "environ.get"})
_CASTS = frozenset({"int", "float", "str", "bool"})
_JS_IMPORT_RE = re.compile(r"import\s*\{([^}]*)\}|import\s+(\w+)\s+from|const\s+(\w+)\s*=\s*require\(")


class ParamFact(BaseModel):
    name: str
    default: str | None = None  # effective default as source text; None means required
    resolved: str | None = None  # literal value when `default` names a module-level constant


class FunctionFact(BaseModel):
    qualname: str
    file: str
    params: list[ParamFact]
    varargs: bool = False  # has *args/**kwargs, so unknown parameter names may be fine

    def describe(self) -> str:
        shown = (p.name if p.default is None else f"{p.name}={p.default}" for p in self.params)
        return f"{self.qualname}({', '.join(shown)})"


class CodeIndex(BaseModel):
    functions: dict[str, list[FunctionFact]] = Field(default_factory=dict)  # by short name
    classes: set[str] = Field(default_factory=set)
    names: set[str] = Field(default_factory=set)  # module-level assignments and imports
    constants: dict[str, str] = Field(default_factory=dict)
    env_vars: set[str] = Field(default_factory=set)
    env_defaults: dict[str, str] = Field(default_factory=dict)  # os.getenv("X", "20") -> X: "20"
    cli_flags: set[str] = Field(default_factory=set)
    routes: set[tuple[str, str]] = Field(default_factory=set)

    def knows(self, identifier: str) -> bool:
        return (
            identifier in self.functions
            or identifier in self.classes
            or identifier in self.names
            or identifier in self.env_vars
        )


def build_code_index(sources: list[SourceFile]) -> CodeIndex:
    index = CodeIndex()
    for source in sources:
        suffix = PurePosixPath(source.path).suffix
        index.env_vars |= extract_env_vars(source.content)
        index.cli_flags |= extract_cli_flags(source.content)
        if suffix in PYTHON_EXTENSIONS:
            tree = parse_python(source.content)
            if tree is not None:
                _index_python(index, source.path, tree)
        elif suffix in JS_EXTENSIONS:
            _index_js(index, source)
    for facts in index.functions.values():
        for fact in facts:
            for param in fact.params:
                if param.default in index.constants:
                    param.resolved = index.constants[param.default]
    return index


def _index_python(index: CodeIndex, path: str, tree: ast.Module) -> None:
    index.env_vars |= extract_settings_env_vars(tree)
    index.routes |= set(extract_python_routes(tree))
    for node in ast.walk(tree):
        default = _env_default(node) if isinstance(node, ast.expr) else None
        name = node.args[0] if isinstance(node, ast.Call) and node.args else None
        if isinstance(default, ast.Constant) and isinstance(name, ast.Constant) and isinstance(name.value, str):
            index.env_defaults[name.value] = str(default.value)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            _add_function(index, _function_fact(node, node.name, path, is_method=False))
        elif isinstance(node, ast.ClassDef):
            index.classes.add(node.name)
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    qualname = f"{node.name}.{child.name}"
                    _add_function(index, _function_fact(child, qualname, path, is_method=True))
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            value = _constant_value(node.value) if node.value is not None else None
            for target in targets:
                if isinstance(target, ast.Name):
                    index.names.add(target.id)
                    if value is not None:
                        index.constants[target.id] = value
        elif isinstance(node, ast.Import):
            index.names |= {alias.asname or alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            index.names |= {alias.asname or alias.name for alias in node.names}


def _index_js(index: CodeIndex, source: SourceFile) -> None:
    index.routes |= extract_routes(source.content)
    for symbol in extract_js_symbols(source.content).values():
        if symbol.kind == SymbolKind.CLASS:
            index.classes.add(symbol.name)
        else:
            params, varargs = _js_params(symbol.signature)
            _add_function(index, FunctionFact(qualname=symbol.name, file=source.path, params=params, varargs=varargs))
    for m in _JS_IMPORT_RE.finditer(source.content):
        if m.group(1):
            index.names |= {n.split(" as ")[-1].strip() for n in m.group(1).split(",") if n.strip()}
        index.names |= {g for g in m.groups()[1:] if g}


def _add_function(index: CodeIndex, fact: FunctionFact) -> None:
    index.functions.setdefault(fact.qualname.rsplit(".", 1)[-1], []).append(fact)


def _function_fact(
    node: ast.FunctionDef | ast.AsyncFunctionDef, qualname: str, path: str, is_method: bool
) -> FunctionFact:
    args = node.args
    positional = args.posonlyargs + args.args
    defaults: list[ast.expr | None] = [None] * (len(positional) - len(args.defaults))
    defaults += list(args.defaults)
    params = [ParamFact(name=a.arg, default=_effective_default(d)) for a, d in zip(positional, defaults)]
    params += [
        ParamFact(name=a.arg, default=_effective_default(d))
        for a, d in zip(args.kwonlyargs, args.kw_defaults)
    ]
    if is_method and params and params[0].name in ("self", "cls"):
        params = params[1:]
    varargs = args.vararg is not None or args.kwarg is not None
    return FunctionFact(qualname=qualname, file=path, params=params, varargs=varargs)


def _effective_default(node: ast.expr | None) -> str | None:
    """`Query(20, ge=1)` -> "20"; `Query(...)` -> None (required); plain defaults unchanged."""
    if node is None:
        return None
    if isinstance(node, ast.Call):
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else ""
        if name in PARAM_WRAPPERS:
            inner = node.args[0] if node.args else next(
                (k.value for k in node.keywords if k.arg == "default"), None
            )
            if inner is None or (isinstance(inner, ast.Constant) and inner.value is Ellipsis):
                return None
            return ast.unparse(inner)
    return ast.unparse(node)


def _constant_value(node: ast.expr) -> str | None:
    """Literal value of a module constant, e.g. `int(os.getenv("X", "20"))` -> "20"."""
    if isinstance(node, ast.Constant) and not isinstance(node.value, bytes):
        return ast.unparse(node)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _CASTS and node.args:
        env_default = _env_default(node.args[0])
        if isinstance(env_default, ast.Constant):
            return str(env_default.value)
        return _constant_value(node.args[0])
    env_default = _env_default(node)
    return ast.unparse(env_default) if isinstance(env_default, ast.Constant) else None


def _env_default(node: ast.expr) -> ast.expr | None:
    if isinstance(node, ast.Call) and ast.unparse(node.func) in _ENV_GETTERS and len(node.args) >= 2:
        return node.args[1]
    return None


def _js_params(signature: str) -> tuple[list[ParamFact], bool]:
    inner = signature[signature.index("(") + 1 : signature.rindex(")")]
    params: list[ParamFact] = []
    varargs = False
    for part in _split_top_level(inner):
        part = part.strip()
        if not part or part[0] in "{[":
            continue
        if part.startswith("..."):
            varargs = True
            continue
        name_part, _, default = part.partition("=")
        name = name_part.split(":")[0].strip().rstrip("?")
        params.append(ParamFact(name=name, default=default.strip() or None))
    return params, varargs


def _split_top_level(text: str) -> list[str]:
    parts, depth, current = [], 0, []
    for char in text:
        if char in "([{<":
            depth += 1
        elif char in ")]}>":
            depth -= 1
        if char == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    parts.append("".join(current))
    return parts
