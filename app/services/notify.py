"""Сообщения в чат организаторов."""
from aiogram import Bot
from redis.asyncio import Redis

from app.config import Settings
from app.logs import get_logger

log = get_logger(__name__)


async def notify_admins(
    bot: Bot,
    settings: Settings,
    text: str,
    *,
    redis: Redis | None = None,
    throttle_key: str | None = None,
    throttle_seconds: int = 600,
    **kwargs,
) -> bool:
    """Шлёт сообщение в ADMIN_CHAT_ID. С throttle_key одинаковые уведомления не чаще раза в throttle_seconds."""
    if not settings.admin_chat_id:
        return False
    if redis is not None and throttle_key:
        if not await redis.set(f"notify:{throttle_key}", "1", nx=True, ex=throttle_seconds):
            return False
    try:
        await bot.send_message(settings.admin_chat_id, text, **kwargs)
        return True
    except Exception as exc:
        log.warning("notify_admins_failed", error=repr(exc))
        return False
