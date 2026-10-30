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
    hubspot_base_url: str = "https://api.hubapi.com"
    hubspot_pipeline: str = "default"
    hubspot_deal_stage: str = "appointmentscheduled"
    hubspot_max_retries: int = 3

    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: SecretStr = SecretStr("")
    smtp_from: str = ""
    # Port 465 uses implicit TLS (SMTP_SSL); other ports use STARTTLS.
    smtp_use_ssl: bool = False
    # Safety net for testing: when set, every approved email goes to this address instead.
    email_redirect_to: str = ""

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
