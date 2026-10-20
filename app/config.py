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

    # Google Sheets (этап 2). Пока SHEET_ID пуст, импорт идёт из CSV, выгрузка — в CSV.
    sheet_id: str = ""
    # Содержимое JSON-ключа сервис-аккаунта целиком (или base64 от него).
    google_service_account_json: str = ""
    # Вкладка, откуда бот берёт участников. По умолчанию — «Прошли отбор»: туда команда переносит
    # строки прошедших из «Кандидаты» в том же формате. Есть строка — есть доступ к боту.
    sheet_applications_worksheet: str = "Прошли отбор"
    # True: каждая строка вкладки-источника = отобран (колонка «Состояние» не нужна), а кого удалили
    # из вкладки — тому доступ закрывается. False: отбор по колонке Selected/«Состояние».
    sheet_source_all_selected: bool = True
    sheet_status_worksheet: str = "Статус бота"  # бот пишет сюда ссылки и статусы + вкладки «Чекин <день>»
    sheet_teams_worksheet: str = ""  # лист "team_name, email"; пусто → не читаем
    # Поле участника → название колонки, если оно не из стандартных (см. DEFAULT_COLUMN_ALIASES в importer.py).
    sheet_column_map: dict[str, str] = {}
    sheet_selected_column: str = "Selected"
    sheet_selected_values: Annotated[frozenset[str], NoDecode] = frozenset(
        {"yes", "да", "1", "true", "+", "✅", "selected", "accepted", "approved",
         "прошел", "прошла", "прошли", "принят", "принята", "одобрен", "одобрена", "отобран", "отобрана"}
    )
    sheet_team_column: str = "Team"
    sheet_sync_minutes: int = 2
    auto_import_minutes: int = 1
    sheets_timeout_seconds: int = 30

    # Дни мероприятия: создаются при старте, если их ещё нет в БД (дальше админ правит командой /day).
    # Время — местное (TIMEZONE).
    event_days: list[dict[str, str]] = [
        {"date": "2026-11-14", "label": "День 1", "opens": "08:00", "closes": "13:00"},
        {"date": "2026-11-15", "label": "День 2", "opens": "08:00", "closes": "13:00"},
    ]

    @field_validator("sheet_selected_values", mode="before")
    @classmethod
    def _parse_values(cls, value: object) -> object:
        if isinstance(value, str):
            return frozenset(x.strip().lower() for x in value.split(",") if x.strip())
        return value

    @property
    def sheets_enabled(self) -> bool:
        return bool(self.sheet_id and self.google_service_account_json)

    @field_validator("superadmin_ids", mode="before")
    @classmethod
    def _parse_ids(cls, value: object) -> object:
        if isinstance(value, str):
            return frozenset(int(x) for x in value.replace(" ", "").split(",") if x)
        return value

    @field_validator("admin_chat_id", mode="before")
    @classmethod
    def _parse_chat_id(cls, value: object) -> object:
        # Терпим то, что обычно копируют из адресной строки web.telegram.org: "#-100123", пробелы, пусто.
        if isinstance(value, str):
            value = value.strip().lstrip("#").replace(" ", "")
            return value or None
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
