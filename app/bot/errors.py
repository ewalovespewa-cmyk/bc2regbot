"""Глобальный обработчик ошибок.

Пользователь получает дружелюбное сообщение, в чат организаторов уходит алерт с трейсбеком.
Одинаковые ошибки (тип + место) алертятся не чаще раза в ERROR_ALERT_THROTTLE_SECONDS.
"""
import hashlib
import html
import traceback

from aiogram import Bot, Router
from aiogram.types import ErrorEvent
from redis.asyncio import Redis

from app.config import Settings
from app.i18n import pick_lang, t
from app.logs import get_logger

log = get_logger(__name__)
router = Router(name="errors")

TG_MESSAGE_LIMIT = 4096


def error_signature(exc: BaseException) -> str:
    tb = traceback.extract_tb(exc.__traceback__)
    where = f"{tb[-1].filename}:{tb[-1].lineno}" if tb else ""
    return hashlib.sha1(f"{type(exc).__name__}|{where}".encode()).hexdigest()[:16]


def format_alert(exc: BaseException, update_id: int | None, user_id: int | None) -> str:
    tb = "".join(traceback.format_exception(exc))
    head = f"⚠️ <b>Ошибка в боте</b>\nupdate_id: {update_id}\nuser_id: {user_id}\n"
    budget = TG_MESSAGE_LIMIT - len(head) - 30
    tail = tb[-budget:]  # самое полезное в конце трейсбека
    return f"{head}<pre>{html.escape(tail)}</pre>"


async def send_error_alert(
    bot: Bot, redis: Redis, settings: Settings, exc: BaseException, update_id: int | None, user_id: int | None
) -> bool:
    """Отправляет алерт, если такая ошибка не алертилась недавно. Возвращает True, если отправил."""
    if not settings.admin_chat_id:
        return False
    key = f"alert:err:{error_signature(exc)}"
    if not await redis.set(key, "1", nx=True, ex=settings.error_alert_throttle_seconds):
        return False
    try:
        await bot.send_message(settings.admin_chat_id, format_alert(exc, update_id, user_id))
    except Exception:
        log.exception("error_alert_failed")
        return False
    return True


@router.errors()
async def on_error(event: ErrorEvent, bot: Bot, redis: Redis, settings: Settings) -> bool:
    update = event.update
    user = None
    chat_id = None
    if update.message:
        user, chat_id = update.message.from_user, update.message.chat.id
    elif update.callback_query:
        user = update.callback_query.from_user
        if update.callback_query.message:
            chat_id = update.callback_query.message.chat.id

    log.error(
        "handler_error",
        update_id=update.update_id,
        user_id=user.id if user else None,
        exc_info=event.exception,
    )

    if chat_id is not None:
        try:
            lang = pick_lang(user.language_code if user else None)
            await bot.send_message(chat_id, t("errors.generic", lang))
        except Exception:
            log.warning("error_reply_failed", chat_id=chat_id)
    if update.callback_query:
        try:
            await update.callback_query.answer()
        except Exception:
            pass

    await send_error_alert(bot, redis, settings, event.exception, update.update_id, user.id if user else None)
    return True
