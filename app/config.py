"""Настройки из переменных окружения. Полное описание каждой переменной — в .env.example."""
from functools import lru_cache
from typing import Annotated

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


def normalize_db_url(url: str) -> str:
    """Railway отдаёт postgres:// или postgresql://, SQLAlchemy нужен драйвер asyncpg."""
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+asyncpg://" + url[len(prefix):]
    return url


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Telegram
    bot_token: str
    bot_username: str = ""  # пусто → берётся из getMe при старте
    webhook_base_url: str = ""  # https://<service>.up.railway.app; пусто → webhook не ставится
    webhook_path: str = "/tg/webhook"
    webhook_secret: str
    port: int = 8080

    # Хранилища
    database_url: str
    redis_url: str

    # Люди и чаты
    superadmin_ids: Annotated[frozenset[int], NoDecode] = frozenset()
    admin_chat_id: int | None = None
    organizer_contact: str = "@businesscup_support"

    # Поведение
    timezone: str = "Asia/Almaty"
    test_mode: bool = False
    log_level: str = "INFO"
    log_json: bool = True
    error_alert_throttle_seconds: int = 300

    # Секреты веб-страниц и интеграций (используются на следующих этапах)
    checkin_secret: str = ""
    screen_secret: str = ""
    dashboard_secret: str = ""
    integration_token: str = ""
    gamification_webhook_url: str = ""
    gamification_webhook_secret: str = ""
    games_bot_username: str = ""
    games_chat_link: str = ""

    @field_validator("superadmin_ids", mode="before")
    @classmethod
    def _parse_ids(cls, value: object) -> object:
        if isinstance(value, str):
            return frozenset(int(x) for x in value.replace(" ", "").split(",") if x)
        return value

    @field_validator("database_url")
    @classmethod
    def _asyncpg_url(cls, value: str) -> str:
        return normalize_db_url(value)

    @field_validator("webhook_base_url")
    @classmethod
    def _strip_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @property
    def webhook_url(self) -> str:
        return f"{self.webhook_base_url}{self.webhook_path}" if self.webhook_base_url else ""


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
