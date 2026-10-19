"""Подозрительные события: пишутся в security_events и (с троттлингом) в чат организаторов."""
import html
from typing import Any

from aiogram import Bot
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import SecurityEvent
from app.logs import get_logger
from app.services.notify import notify_admins

log = get_logger(__name__)

TOKEN_OWNED_BY_OTHER = "token_owned_by_other"
ACCOUNT_LINKED_TO_OTHER = "account_linked_to_other"
FOREIGN_CONTACT = "foreign_contact"
AMBIGUOUS_PHONE = "ambiguous_phone"
INVALID_TOKEN_FLOOD = "invalid_token_flood"
INVALID_CODE_FLOOD = "invalid_code_flood"
HELP_AT_ENTRANCE = "help_at_entrance"

TITLES = {
    TOKEN_OWNED_BY_OTHER: "Попытка привязать чужую ссылку",
    ACCOUNT_LINKED_TO_OTHER: "Аккаунт уже привязан к другому участнику",
    FOREIGN_CONTACT: "Прислан чужой контакт",
    AMBIGUOUS_PHONE: "Один номер у нескольких участников",
    INVALID_TOKEN_FLOOD: "Много попыток с неверными ссылками",
    INVALID_CODE_FLOOD: "Много попыток чекина с неверным QR",
    HELP_AT_ENTRANCE: "🙋 Нужна помощь на входе",
}


async def report(
    session: AsyncSession,
    *,
    bot: Bot,
    redis: Redis,
    settings: Settings,
    kind: str,
    telegram_user_id: int | None,
    username: str | None = None,
    participant_id: int | None = None,
    notify: bool = True,
    **details: Any,
) -> None:
    session.add(
        SecurityEvent(kind=kind, telegram_user_id=telegram_user_id, participant_id=participant_id, details=details)
    )
    log.warning("security_event", kind=kind, telegram_user_id=telegram_user_id, **details)
    if not notify:
        return
    who = f"@{html.escape(username)}" if username else "без username"
    lines = [f"🚨 <b>{TITLES.get(kind, kind)}</b>", f"Telegram: <code>{telegram_user_id}</code> ({who})"]
    lines += [f"{html.escape(str(k))}: {html.escape(str(v))}" for k, v in details.items()]
    await notify_admins(
        bot, settings, "\n".join(lines), redis=redis, throttle_key=f"sec:{kind}:{telegram_user_id}", throttle_seconds=900
    )
