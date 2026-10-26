"""Application settings loaded from environment variables / .env."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    github_token: str = ""
    webhook_secret: str = ""

    llm_base_url: str = "https://api.groq.com/openai/v1"
    llm_api_key: str = ""
    llm_model: str = "openai/gpt-oss-120b"
    # Groq's free tier allows 8000 tokens/min for gpt-oss-120b; 0 disables client-side pacing.
    llm_tpm_limit: int = 8000
    # Sent as `reasoning_effort` (low|medium|high) when set; empty to omit for other providers.
    llm_reasoning_effort: str = "low"

    database_url: str = "sqlite:///driftless.db"


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance."""
    return Settings()
