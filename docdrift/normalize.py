"""Small text normalizers shared by the reviewer and the doc editor."""


def normalize_heading(heading: str) -> str:
    """Compare headings ignoring backticks, leading #'s, case and spacing."""
    return " ".join(heading.replace("`", "").lstrip("#").split()).casefold()
