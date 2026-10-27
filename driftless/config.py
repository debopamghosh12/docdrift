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

    # Web app: repos it may act on with GITHUB_TOKEN, comma-separated `owner/name` or `owner/*`.
    # Empty: none (the webhook is unaffected).
    allowed_repos: str = ""
    # Shown as shortcuts on the home page, besides the exact ALLOWED_REPOS rules.
    featured_repos: str = ""

    # Public demo: anyone may browse and run read-only jobs; writes to GitHub need an admin
    # session (log in with ADMIN_TOKEN). Job caps and a per-IP rate limit keep it small.
    public_demo: bool = False
    admin_token: str = ""
    demo_max_jobs: int = 2
    demo_max_jobs_per_repo: int = 1
    demo_rate_limit: int = 5  # job starts per IP per window
    demo_rate_window_s: int = 600
    # Header with the client IP set by the host's proxy (e.g. X-Forwarded-For on Render); its last
    # entry is used. Empty: the socket peer.
    client_ip_header: str = ""

    # Executable docs: run documented examples against the app (runs the repo's code locally).
    execute_examples: bool = True
    sandbox_cache_dir: str = ".driftless_cache"
    sandbox_startup_timeout: float = 30.0
    sandbox_install_timeout: float = 600.0
    example_request_timeout: float = 10.0
    # Linux, server running as root: installs and apps under test run as this unprivileged user
    # (the Docker image sets it). Empty: they run as the server's own user (see SECURITY.md).
    sandbox_user: str = ""


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance."""
    return Settings()
