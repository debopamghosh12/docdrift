from typer.testing import CliRunner

from driftless.cli import app
from driftless.config import Settings


def test_cli_help() -> None:
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "Driftless" in result.output


def test_settings_defaults() -> None:
    settings = Settings(_env_file=None)
    assert settings.llm_model == "openai/gpt-oss-120b"
