"""`.driftless.yml`: parsing, the memory it gives every run, and Action-mode command replies."""

import pytest
import yaml

from driftless.commands import parse_command
from driftless.repo_config import RepoConfig, RepoConfigError, action_memory_reply, config_memory, parse_repo_config

REPO = "acme/notes"


def test_parses_ignore_and_style_lists() -> None:
    config = parse_repo_config("ignore:\n  - CHANGELOG.md\n  - Legacy API\nstyle:\n  - Use British spelling.\n")
    assert config == RepoConfig(ignore=["CHANGELOG.md", "Legacy API"], style=["Use British spelling."])


@pytest.mark.parametrize("text", [None, "", "  \n", "# just a comment\n", "ignore:\n"])
def test_missing_or_empty_file_is_an_empty_config(text: str | None) -> None:
    assert parse_repo_config(text) == RepoConfig()


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("ignore: [unclosed", "not valid YAML"),
        ("- CHANGELOG.md\n", "must be a mapping"),
        ("ignored:\n  - CHANGELOG.md\n", "unknown keys: ignored"),
        ("ignore: CHANGELOG.md\n", "must be lists of strings"),
    ],
)
def test_malformed_files_say_what_is_wrong(text: str, message: str) -> None:
    with pytest.raises(RepoConfigError, match=message):
        parse_repo_config(text)


def test_config_memory_applies_like_remembered_rules() -> None:
    memory = config_memory(REPO, RepoConfig(ignore=["CHANGELOG.md"], style=["Keep examples minimal."]))
    remembered = memory.repo_memory(REPO)
    assert remembered.ignore == ["CHANGELOG.md"]
    assert remembered.style == ["Keep examples minimal."]
    assert remembered.ignoring_file_rule("CHANGELOG.md") == "CHANGELOG.md"


def reply(command: str, config: RepoConfig) -> str:
    parsed = parse_command(command)
    assert parsed is not None
    return action_memory_reply("dev", parsed, config)


def yaml_in(text: str) -> object:
    block = text.split("```yaml\n", 1)[1].split("```", 1)[0]
    return yaml.safe_load(block)


def test_ignore_reply_is_the_whole_file_with_the_new_rule() -> None:
    text = reply("/driftless ignore docs/*.md", RepoConfig(style=["Use: colons # and hashes"]))
    assert yaml_in(text) == {"ignore": ["docs/*.md"], "style": ["Use: colons # and hashes"]}


def test_style_reply_adds_under_style() -> None:
    text = reply("/driftless style keep examples minimal", RepoConfig(ignore=["CHANGELOG.md"]))
    assert yaml_in(text) == {"ignore": ["CHANGELOG.md"], "style": ["keep examples minimal"]}


def test_already_ignored_rule_says_so() -> None:
    assert "already ignored" in reply("/driftless ignore CHANGELOG.md", RepoConfig(ignore=["CHANGELOG.md"]))


def test_unignore_and_unstyle_show_the_line_to_delete() -> None:
    config = RepoConfig(ignore=["Legacy API"], style=["first", "second"])
    assert "delete this line" in reply("/driftless unignore Legacy API", config)
    assert "  - Legacy API" in reply("/driftless unignore Legacy API", config)
    assert "  - second" in reply("/driftless unstyle 2", config)
    assert "has no style preference #3" in reply("/driftless unstyle 3", config)
    assert "has no ignore rule `nope`" in reply("/driftless unignore nope", config)


def test_memory_reply_shows_the_current_file() -> None:
    assert yaml_in(reply("/driftless memory", RepoConfig(ignore=["a.md"]))) == {"ignore": ["a.md"]}
    assert "(no rules yet)" in reply("/driftless memory", RepoConfig())
