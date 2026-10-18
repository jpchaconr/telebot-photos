"""Application settings loaded from environment variables (or a ``.env`` file)."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from cryptography.fernet import Fernet
from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# Biggest file a bot can download through Telegram's cloud Bot API.
CLOUD_BOT_API_DOWNLOAD_LIMIT = 20 * 1024 * 1024


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    telegram_bot_token: str
    # URL of a self-hosted telegram-bot-api server (e.g. http://telegram-bot-api:8081).
    # When empty the official cloud Bot API is used and downloads are limited to 20 MB.
    bot_api_url: str | None = None
    # Public hostname of the server (e.g. nombre.duckdns.org), also used by Caddy.
    domain: str | None = None
    # Public HTTPS URL where the OAuth callbacks are reachable. Defaults to https://DOMAIN.
    public_base_url: str = ""
    # Fernet key used to encrypt the credentials stored for every chat.
    encryption_key: str

    database_path: Path = Path("data/bot.db")
    temp_dir: Path = Path("data/tmp")
    web_host: str = "0.0.0.0"
    web_port: int = 8080

    google_client_id: str | None = None
    google_client_secret: str | None = None
    ms_client_id: str | None = None
    ms_client_secret: str | None = None
    ms_tenant: str = "common"

    timezone: str = "UTC"
    upload_concurrency: int = Field(default=3, ge=1, le=32)
    max_attempts: int = Field(default=5, ge=1)
    success_reaction: str = "👍"
    # Telegram user ids allowed to run /configurar. Empty means any group administrator.
    allowed_configurators: Annotated[list[int], NoDecode] = []
    log_level: str = "INFO"
    # Shown on the public privacy page.
    contact_email: str | None = None
    # Set by the Docker image build; reported by /health.
    app_version: str = "dev"

    @field_validator("allowed_configurators", mode="before")
    @classmethod
    def _split_ids(cls, value: object) -> object:
        if isinstance(value, str):
            return [int(part) for part in value.replace(";", ",").split(",") if part.strip()]
        return value

    @field_validator("bot_api_url", "public_base_url", mode="after")
    @classmethod
    def _strip_slash(cls, value: str | None) -> str | None:
        return value.rstrip("/") if value else value

    @field_validator("encryption_key")
    @classmethod
    def _check_key(cls, value: str) -> str:
        try:
            Fernet(value.encode())
        except ValueError as exc:
            raise ValueError(
                "ENCRYPTION_KEY no es una clave Fernet válida. Genera una con: "
                "openssl rand -base64 32 | tr '+/' '-_'"
            ) from exc
        return value

    @model_validator(mode="after")
    def _derive_public_url(self) -> Settings:
        if not self.public_base_url:
            if not self.domain:
                raise ValueError("Define DOMAIN (o PUBLIC_BASE_URL) con la dirección pública")
            self.public_base_url = f"https://{self.domain.strip().strip('/')}"
        return self

    @field_validator("timezone")
    @classmethod
    def _check_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"Zona horaria desconocida: {value}") from exc
        return value

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def local_bot_api(self) -> bool:
        return bool(self.bot_api_url)

    @property
    def max_download_size(self) -> int | None:
        """Biggest file the bot can fetch from Telegram (None: no limit worth checking)."""
        return None if self.local_bot_api else CLOUD_BOT_API_DOWNLOAD_LIMIT

    @property
    def google_enabled(self) -> bool:
        return bool(self.google_client_id and self.google_client_secret)

    @property
    def microsoft_enabled(self) -> bool:
        return bool(self.ms_client_id and self.ms_client_secret)

    def redirect_uri(self, provider: str) -> str:
        return f"{self.public_base_url}/oauth/{provider}/callback"
