from typer.testing import CliRunner

from docdrift.cli import app
from docdrift.config import Settings


def test_cli_help() -> None:
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "DocDrift" in result.output


def test_settings_defaults() -> None:
    settings = Settings(_env_file=None)
    assert settings.llm_model == "openai/gpt-oss-120b"
