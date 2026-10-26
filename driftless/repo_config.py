"""`.driftless.yml`: a repo's memory in Action mode, where SQLite doesn't outlive the runner.

```yaml
ignore:            # files, folders, globs or headings, like `/driftless ignore`
  - CHANGELOG.md
  - docs/legacy/
  - Legacy API
style:             # notes given to the model, like `/driftless style`
  - Use British spelling.
```

It is read from the repo's default branch, so a PR can't loosen the rules it is checked against.
"""

import json

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator

from driftless.commands import Command, CommandName
from driftless.memory import Memory

CONFIG_FILE = ".driftless.yml"


class RepoConfigError(ValueError):
    pass


class RepoConfig(BaseModel):
    ignore: list[str] = Field(default_factory=list)
    style: list[str] = Field(default_factory=list)

    @field_validator("ignore", "style", mode="before")
    @classmethod
    def _strings(cls, value: object) -> object:
        if value is None:
            return []
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        return value


def parse_repo_config(text: str | None) -> RepoConfig:
    """An absent or empty file is an empty config; anything malformed raises RepoConfigError."""
    if not text or not text.strip():
        return RepoConfig()
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise RepoConfigError(f"{CONFIG_FILE} is not valid YAML: {exc}") from exc
    if data is None:
        return RepoConfig()
    if not isinstance(data, dict):
        raise RepoConfigError(f"{CONFIG_FILE} must be a mapping with `ignore:` and `style:` lists")
    unknown = sorted(set(data) - {"ignore", "style"})
    if unknown:
        raise RepoConfigError(f"{CONFIG_FILE} has unknown keys: {', '.join(unknown)} (expected `ignore`, `style`)")
    try:
        return RepoConfig.model_validate(data)
    except ValidationError as exc:
        raise RepoConfigError(f"{CONFIG_FILE}: `ignore` and `style` must be lists of strings") from exc


def config_memory(repo: str, config: RepoConfig) -> Memory:
    """A throwaway in-memory Memory holding the file's rules, so every run uses them unchanged."""
    memory = Memory("sqlite://")
    for rule in config.ignore:
        memory.add_ignore(repo, rule, CONFIG_FILE)
    for note in config.style:
        memory.add_style(repo, note, CONFIG_FILE)
    return memory


def action_memory_reply(user: str, command: Command, config: RepoConfig) -> str:
    """The reply to `/driftless ignore|unignore|style|unstyle|memory` in Action mode: the exact
    `.driftless.yml` change to make (the runner can't remember anything itself)."""
    intro = f"🧠 @{user} in GitHub Action mode, Driftless's memory lives in `{CONFIG_FILE}` on the default branch."
    argument = command.argument
    if command.name == CommandName.MEMORY:
        return f"{intro} It currently says:\n\n{_yaml_block(config)}"
    if command.name == CommandName.IGNORE:
        if argument in config.ignore:
            return f"🧠 @{user} `{argument}` is already ignored in `{CONFIG_FILE}`."
        return f"{intro} To ignore `{argument}`, add it under `ignore:`:\n\n{_yaml_block(config, ignore=[argument])}"
    if command.name == CommandName.STYLE:
        return f"{intro} To add this style preference, add it under `style:`:\n\n{_yaml_block(config, style=[argument])}"
    if command.name == CommandName.UNIGNORE:
        if argument not in config.ignore:
            return f"🧠 @{user} `{CONFIG_FILE}` has no ignore rule `{argument}`.\n\n{_yaml_block(config)}"
        return f"{intro} To stop ignoring `{argument}`, delete this line from `ignore:`:\n\n```yaml\n  - {_scalar(argument)}\n```"
    if command.name == CommandName.UNSTYLE:
        index = int(argument) - 1 if argument.isdigit() else -1
        if not 0 <= index < len(config.style):
            return f"🧠 @{user} `{CONFIG_FILE}` has no style preference #{argument}.\n\n{_yaml_block(config)}"
        line = _scalar(config.style[index])
        return f"{intro} To remove preference #{argument}, delete this line from `style:`:\n\n```yaml\n  - {line}\n```"
    raise ValueError(f"not a memory command: {command.text}")


def _yaml_block(config: RepoConfig, ignore: list[str] | None = None, style: list[str] | None = None) -> str:
    """The whole file as it should read, new lines included, in a ```yaml block."""
    lines = []
    for key, current, added in (("ignore", config.ignore, ignore or []), ("style", config.style, style or [])):
        if current or added:
            lines.append(f"{key}:")
            lines += [f"  - {_scalar(item)}" for item in current]
            lines += [f"  - {_scalar(item)}   # new" for item in added]
    body = "\n".join(lines) or "# (no rules yet)"
    return f"```yaml\n# {CONFIG_FILE}\n{body}\n```"


def _scalar(text: str) -> str:
    """A YAML scalar for `text`: bare when it reads back unchanged, else JSON-quoted (valid YAML)."""
    try:
        if yaml.safe_load(text) == text and "#" not in text:
            return text
    except yaml.YAMLError:
        pass
    return json.dumps(text, ensure_ascii=False)
