"""Environment for the app under test: never override a default the code defines.

Setting the docs' documented defaults would mask exactly the drift we run examples to find: if
the code's `MAX_BODY_LENGTH` default changed from 1000 to 10, starting the app with the docs'
1000 makes the old example pass. So only variables the code *requires* (no default in code) are
set, to the documented value when there is one, else a placeholder of the right type. Variables
the code reads are also removed from the inherited host environment, for the same reason.
"""

import ast
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from driftless.diff_parser.patterns import extract_env_vars
from driftless.diff_parser.python_symbols import _env_prefix, extract_settings_env_vars
from driftless.examples import documented_env_defaults

_ENVIRON = frozenset({"os.environ", "environ"})
_PLACEHOLDERS = {"int": "1", "float": "1.0", "bool": "false"}
PLACEHOLDER = "driftless-placeholder"


@dataclass
class AppEnv:
    set: dict[str, str] = field(default_factory=dict)  # required by the code, no default
    unset: frozenset[str] = frozenset()  # read by the code: never inherited from the host


def app_env(files: dict[str, str], docs: dict[str, str]) -> AppEnv:
    """`files`: the repo snapshot the app starts from; `docs`: doc files as they will be run."""
    documented: dict[str, str] = {}
    for _, text in sorted(docs.items()):
        documented.update(documented_env_defaults(text))
    required: dict[str, str] = {}
    read: set[str] = set()
    for path, content in files.items():
        if PurePosixPath(path).suffix != ".py":
            continue
        read |= extract_env_vars(content)
        try:
            tree = ast.parse(content)
        except SyntaxError:
            continue
        read |= extract_settings_env_vars(tree)
        required.update(required_env_vars(tree))
    values = {name: documented.get(name, placeholder) for name, placeholder in required.items()}
    return AppEnv(set=values, unset=frozenset(read | set(required)))


def required_env_vars(tree: ast.Module) -> dict[str, str]:
    """{name: placeholder} for `os.environ["X"]` reads and settings fields without a default."""
    required: dict[str, str] = {}
    casts: dict[int, str] = {}  # id(subscript) -> cast it is wrapped in, e.g. int(os.environ["X"])
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.args:
            casts[id(node.args[0])] = node.func.id
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.ctx, ast.Load)
            and ast.unparse(node.value) in _ENVIRON
            and isinstance(node.slice, ast.Constant)
            and isinstance(node.slice.value, str)
        ):
            required[node.slice.value] = _PLACEHOLDERS.get(casts.get(id(node), ""), PLACEHOLDER)
        elif isinstance(node, ast.ClassDef) and any(ast.unparse(b).endswith("BaseSettings") for b in node.bases):
            prefix = _env_prefix(node)
            for stmt in node.body:
                if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name) and _no_default(stmt):
                    name = f"{prefix}{stmt.target.id}".upper()
                    required[name] = _PLACEHOLDERS.get(ast.unparse(stmt.annotation), PLACEHOLDER)
    return required


def _no_default(stmt: ast.AnnAssign) -> bool:
    if stmt.value is None:
        return not isinstance(stmt.target, ast.Name) or not stmt.target.id.startswith("_")
    value = stmt.value
    if isinstance(value, ast.Call) and ast.unparse(value.func).endswith("Field"):
        default = value.args[0] if value.args else next((k.value for k in value.keywords if k.arg == "default"), None)
        has_factory = any(k.arg == "default_factory" for k in value.keywords)
        return not has_factory and (default is None or (isinstance(default, ast.Constant) and default.value is Ellipsis))
    return False
