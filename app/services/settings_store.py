"""Переключатели рантайма в таблице settings: админ меняет их из бота, без деплоя."""
from typing import Any

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Setting

AUTO_IMPORT_ENABLED = "auto_import_enabled"


async def get_setting(session: AsyncSession, key: str, default: Any = None) -> Any:
    row = await session.get(Setting, key)
    return default if row is None else row.value


async def set_setting(session: AsyncSession, key: str, value: Any) -> None:
    stmt = insert(Setting).values(key=key, value=value)
    await session.execute(stmt.on_conflict_do_update(index_elements=[Setting.key], set_={"value": stmt.excluded.value}))
