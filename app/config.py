from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

Effort = Literal["low", "medium", "high", "xhigh", "max"]


class Settings(BaseSettings):
    """Application settings loaded from environment variables and the .env file.

    Secrets use SecretStr so they are masked when printed or logged.
    """

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    anthropic_api_key: SecretStr = SecretStr("")
    anthropic_model: str = "claude-opus-5-5"
    anthropic_effort: Effort = "medium"

    hubspot_token: SecretStr = SecretStr("")

    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: SecretStr = SecretStr("")
    smtp_from: str = ""

    # When true, no real emails are sent and nothing is written to the CRM.
    dry_run: bool = True

    # Agent loop
    agent_max_steps: int = 10
    icp_path: Path = Path("config/icp.yaml")
    fetch_max_chars: int = 8000
    http_timeout_seconds: float = 10.0

    database_url: str = "sqlite:///data/app.db"
    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance."""
    return Settings()
