"""Load LLM prompt templates shipped with the package (`driftless/prompts/*.txt`)."""

from functools import lru_cache
from importlib.resources import files
from string import Template


@lru_cache
def load_prompt(name: str) -> Template:
    """Templates use `$name` placeholders so literal JSON braces need no escaping."""
    return Template(files(__package__).joinpath(f"{name}.txt").read_text(encoding="utf-8"))


def render_prompt(template: str, /, **values: str) -> str:
    # Positional-only so templates may use any placeholder name, including `$name`.
    return load_prompt(template).substitute(**values)
