"""Language-agnostic symbol definitions and before/after comparison."""

import re
from dataclasses import dataclass

from docdrift.models import ChangeKind, SymbolChange, SymbolKind


@dataclass(frozen=True)
class SymbolDef:
    """A function/class/method definition. `body_key` is None when the body can't be compared."""

    name: str
    kind: SymbolKind
    signature: str
    body_key: str | None = None


def short_name(qualname: str) -> str:
    return qualname.rsplit(".", 1)[-1]


def is_public(qualname: str) -> bool:
    name = short_name(qualname)
    is_dunder = name.startswith("__") and name.endswith("__")
    return is_dunder or not name.startswith("_")


def diff_symbols(
    before: dict[str, SymbolDef],
    after: dict[str, SymbolDef],
    file: str,
    touched: str = "",
) -> list[SymbolChange]:
    """Compare two symbol tables. `touched` is hunk-header text used when bodies aren't comparable."""
    changes: list[SymbolChange] = []
    for name in sorted(before.keys() & after.keys()):
        change = _compare(before[name], after[name], touched)
        if change:
            changes.append(_make_change(change, file, before[name], after[name]))

    removed = [before[n] for n in sorted(before.keys() - after.keys())]
    added = [after[n] for n in sorted(after.keys() - before.keys())]
    renames = _pair_renames(removed, added)
    renamed_old = {old.name for old, _ in renames}
    renamed_new = {new.name for _, new in renames}

    changes.extend(_make_change(ChangeKind.RENAMED, file, old, new) for old, new in renames)
    changes.extend(
        _make_change(ChangeKind.REMOVED, file, s, None) for s in removed if s.name not in renamed_old
    )
    changes.extend(
        _make_change(ChangeKind.ADDED, file, None, s) for s in added if s.name not in renamed_new
    )
    return changes


def _compare(old: SymbolDef, new: SymbolDef, touched: str) -> ChangeKind | None:
    if old.signature != new.signature:
        return ChangeKind.SIGNATURE_CHANGED
    if old.body_key is not None and new.body_key is not None:
        return ChangeKind.MODIFIED if old.body_key != new.body_key else None
    pattern = rf"(?<![\w$]){re.escape(short_name(new.name))}(?![\w$])"
    if touched and re.search(pattern, touched):
        return ChangeKind.MODIFIED
    return None


def _parent(qualname: str) -> str:
    return qualname.rsplit(".", 1)[0] if "." in qualname else ""


def _pair_renames(
    removed: list[SymbolDef], added: list[SymbolDef]
) -> list[tuple[SymbolDef, SymbolDef]]:
    """Pair a removed and an added symbol when their bodies are identical and the match is unique."""
    pairs: list[tuple[SymbolDef, SymbolDef]] = []
    used: set[str] = set()
    for old in removed:
        if not old.body_key:
            continue
        matches = [
            new
            for new in added
            if new.name not in used
            and new.kind == old.kind
            and new.body_key == old.body_key
            and _parent(new.name) == _parent(old.name)
        ]
        if len(matches) == 1:
            pairs.append((old, matches[0]))
            used.add(matches[0].name)
    return pairs


def _make_change(
    change: ChangeKind, file: str, old: SymbolDef | None, new: SymbolDef | None
) -> SymbolChange:
    current = new or old
    assert current is not None
    return SymbolChange(
        name=current.name,
        kind=current.kind,
        change=change,
        file=file,
        old_name=old.name if old and new and old.name != new.name else None,
        old_signature=old.signature if old else None,
        new_signature=new.signature if new else None,
        public=is_public(current.name),
    )
