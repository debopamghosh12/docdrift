"""Turn a PR's changed files into a structured ChangeSet of doc-relevant changes."""

from pathlib import PurePosixPath
from typing import TypeVar

from docdrift.diff_parser.patch import hunk_headers, parse_unified_diff, reconstruct
from docdrift.diff_parser.patterns import extract_cli_flags, extract_env_vars, extract_routes
from docdrift.diff_parser.python_symbols import (
    extract_python_routes,
    extract_python_symbols,
    extract_settings_env_vars,
    parse_python,
)
from docdrift.diff_parser.regex_symbols import extract_js_symbols, extract_python_symbols_regex
from docdrift.diff_parser.routes import RouteTable, diff_routes, tag_route_handlers
from docdrift.diff_parser.symbols import diff_symbols
from docdrift.models import (
    ChangedFile,
    ChangeKind,
    ChangeSet,
    CliFlagChange,
    EnvVarChange,
    FileStatus,
)

__all__ = ["build_changeset", "is_source_path", "is_test_path", "parse_unified_diff"]

PYTHON_EXTENSIONS = frozenset({".py", ".pyi"})
JS_EXTENSIONS = frozenset({".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"})
_TEST_DIRS = frozenset({"test", "tests", "__tests__", "spec"})

T = TypeVar("T", str, tuple[str, str])


def is_source_path(path: str) -> bool:
    return PurePosixPath(path).suffix in PYTHON_EXTENSIONS | JS_EXTENSIONS


def is_test_path(path: str) -> bool:
    p = PurePosixPath(path)
    return (
        any(part in _TEST_DIRS for part in p.parts[:-1])
        or p.name.startswith("test_")
        or ".test." in p.name
        or ".spec." in p.name
    )


def build_changeset(files: list[ChangedFile]) -> ChangeSet:
    changeset = ChangeSet(files=[f.path for f in files])
    for changed in files:
        if is_source_path(changed.path) and not is_test_path(changed.path):
            _analyze_file(changed, changeset)
    return changeset


def _analyze_file(changed: ChangedFile, changeset: ChangeSet) -> None:
    before, after = _file_texts(changed)
    env_before, env_after = extract_env_vars(before), extract_env_vars(after)
    # Regex routes don't know their handler; the Python AST path below replaces them.
    routes_before: RouteTable = dict.fromkeys(extract_routes(before))
    routes_after: RouteTable = dict.fromkeys(extract_routes(after))

    if PurePosixPath(changed.path).suffix in PYTHON_EXTENSIONS:
        tree_before, tree_after = parse_python(before), parse_python(after)
        if tree_before is not None and tree_after is not None:
            symbols_before = extract_python_symbols(tree_before)
            symbols_after = extract_python_symbols(tree_after)
            env_before |= extract_settings_env_vars(tree_before)
            env_after |= extract_settings_env_vars(tree_after)
            routes_before = dict(extract_python_routes(tree_before))
            routes_after = dict(extract_python_routes(tree_after))
        else:
            symbols_before = extract_python_symbols_regex(before)
            symbols_after = extract_python_symbols_regex(after)
    else:
        symbols_before, symbols_after = extract_js_symbols(before), extract_js_symbols(after)

    path = changed.path
    symbol_changes = diff_symbols(
        symbols_before, symbols_after, path, touched=hunk_headers(changed.patch)
    )
    tag_route_handlers(symbol_changes, routes_before, routes_after)
    changeset.symbols.extend(symbol_changes)
    changeset.routes.extend(
        diff_routes(routes_before, routes_after, {s.name for s in symbol_changes}, path)
    )
    changeset.env_vars.extend(
        EnvVarChange(name=name, change=change, file=path)
        for name, change in _set_changes(env_before, env_after)
    )
    changeset.cli_flags.extend(
        CliFlagChange(flag=flag, change=change, file=path)
        for flag, change in _set_changes(extract_cli_flags(before), extract_cli_flags(after))
    )


def _file_texts(changed: ChangedFile) -> tuple[str, str]:
    """Prefer full file content; fall back to text reconstructed from the patch."""
    has_before = changed.before is not None or changed.status == FileStatus.ADDED
    has_after = changed.after is not None or changed.status == FileStatus.REMOVED
    if has_before and has_after:
        return changed.before or "", changed.after or ""
    return reconstruct(changed.patch)


def _set_changes(before: set[T], after: set[T]) -> list[tuple[T, ChangeKind]]:
    removed = [(item, ChangeKind.REMOVED) for item in sorted(before - after)]
    added = [(item, ChangeKind.ADDED) for item in sorted(after - before)]
    return removed + added

