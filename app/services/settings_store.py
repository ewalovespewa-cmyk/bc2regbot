"""Переключатели рантайма в таблице settings: админ меняет их из бота, без деплоя."""
from typing import Any

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Setting

# Режим мероприятия: сбросы, удаление дней и импорт файлов заблокированы, из таблицы людей только добавляем.
EVENT_LOCK = "event_lock"
ATTENDANCE_POLL_SENT = "attendance_poll_sent"  # запланированный опрос уже разослан
AUTO_IMPORT_ENABLED = "auto_import_enabled"
AUTO_IMPORT_DEFAULT = True  # если Google Sheet подключён, автоимпорт включён, пока его не выключат


async def get_setting(session: AsyncSession, key: str, default: Any = None) -> Any:
    row = await session.get(Setting, key)
    return default if row is None else row.value


async def claim_setting(session: AsyncSession, key: str, value: Any) -> bool:
    """Записывает значение, только если его ещё нет. True — записали мы."""
    stmt = insert(Setting).values(key=key, value=value).on_conflict_do_nothing(index_elements=[Setting.key])
    return await session.scalar(stmt.returning(Setting.key)) is not None


async def set_setting(session: AsyncSession, key: str, value: Any) -> None:
    stmt = insert(Setting).values(key=key, value=value)
    await session.execute(stmt.on_conflict_do_update(index_elements=[Setting.key], set_={"value": stmt.excluded.value}))
