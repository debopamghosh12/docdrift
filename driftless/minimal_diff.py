"""Keep LLM rewrites minimal: undo cosmetic churn and measure unrelated changes."""

import re
from difflib import SequenceMatcher

_SEPARATOR_CELL_RE = re.compile(r"^:?-+:?$")
# Typographic look-alikes LLMs substitute for plain characters (non-breaking hyphens/spaces,
# curly quotes). Folded only for comparison; the original characters are what gets restored.
_LOOKALIKES = str.maketrans(
    {"‐": "-", "‑": "-", " ": " ", " ": " ", "‘": "'", "’": "'",
     "“": '"', "”": '"'}
)
UNRELATED_RATIO_LIMIT = 0.4
MIN_LINES_FOR_RATIO = 4


def restore_formatting(original: str, updated: str) -> tuple[str, int]:
    """Restore original lines that the rewrite changed only in whitespace or table padding.

    New table rows are re-padded to the original column widths. Returns the text and the
    number of lines restored.
    """
    old, new = original.splitlines(), updated.splitlines()
    matcher = SequenceMatcher(None, [_normalize(l) for l in old], [_normalize(l) for l in new], autojunk=False)
    lines: list[str] = []
    from_original: list[bool] = []
    restored = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                restored += old[i1 + k] != new[j1 + k]
                lines.append(old[i1 + k])
                from_original.append(True)
        else:
            lines.extend(new[j1:j2])
            from_original.extend([False] * (j2 - j1))
    _repad_new_table_rows(lines, from_original)
    trailing = "\n" if updated.endswith("\n") else ""
    return "\n".join(lines) + trailing, restored


def unrelated_change_ratio(original: str, updated: str, changed_names: list[str]) -> tuple[int, int]:
    """(original lines changed that mention no changed name, non-blank original lines)."""
    old, new = original.splitlines(), updated.splitlines()
    matcher = SequenceMatcher(None, old, new, autojunk=False)
    unrelated = 0
    for tag, i1, i2, _, _ in matcher.get_opcodes():
        if tag in ("replace", "delete"):
            unrelated += sum(
                1 for line in old[i1:i2] if line.strip() and not any(n in line for n in changed_names)
            )
    return unrelated, sum(1 for line in old if line.strip())


def unrelated_change_flag(original: str, updated: str, changed_names: list[str]) -> str | None:
    unrelated, total = unrelated_change_ratio(original, updated, changed_names)
    if total >= MIN_LINES_FOR_RATIO and unrelated / total > UNRELATED_RATIO_LIMIT:
        return (
            f"rewrote {unrelated} of {total} original lines that mention none of the changed "
            f"names ({unrelated / total:.0%}); review for unrelated edits"
        )
    return None


def _normalize(line: str) -> str:
    stripped = line.translate(_LOOKALIKES).strip()
    if stripped.startswith("|"):
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        return "|".join("---" if _SEPARATOR_CELL_RE.match(c) else c for c in cells)
    return " ".join(stripped.split())


def _repad_new_table_rows(lines: list[str], from_original: list[bool]) -> None:
    """Pad new rows' cells (except the last) to the widths of an original separator row."""
    widths: list[int] | None = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped.startswith("|"):
            widths = None
            continue
        cells = stripped.strip("|").split("|")
        if all(_SEPARATOR_CELL_RE.match(c.strip()) for c in cells):
            widths = [len(c) for c in cells] if from_original[i] else None
            continue
        if widths is None or from_original[i] or len(cells) != len(widths):
            continue
        padded = []
        for j, cell in enumerate(cells):
            content = cell.strip()
            is_last = j == len(cells) - 1
            if not is_last and len(content) <= widths[j] - 2:
                padded.append(f" {content.ljust(widths[j] - 2)} ")
            else:
                padded.append(f" {content} ")
        lines[i] = "|" + "|".join(padded) + "|"
