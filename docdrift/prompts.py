"""Load LLM prompt templates from the repo's prompts/ folder."""

from functools import lru_cache
from pathlib import Path
from string import Template

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"


@lru_cache
def load_prompt(name: str) -> Template:
    """Templates use `$name` placeholders so literal JSON braces need no escaping."""
    return Template((PROMPTS_DIR / f"{name}.txt").read_text(encoding="utf-8"))


def render_prompt(template: str, /, **values: str) -> str:
    # Positional-only so templates may use any placeholder name, including `$name`.
    return load_prompt(template).substitute(**values)
