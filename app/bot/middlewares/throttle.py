"""Анти-спам: не больше THROTTLE_LIMIT апдейтов от одного человека за THROTTLE_WINDOW_SECONDS.

Лишние апдейты отбрасываются до открытия сессии БД и без ответа в Telegram, поэтому
человек, который жмёт кнопку без остановки, не тормозит бота остальным. Один раз за окно
он получает «не так быстро». Альбомы (одна отправка — много сообщений) и группы не считаются.
Скан QR на входе тоже не считается: отметка важнее. Если Redis недоступен, апдейт пропускаем:
лучше без защиты, чем бот, который молчит.
"""
import time
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware, Bot
from aiogram.types import TelegramObject, Update
from redis.asyncio import Redis

from app.i18n import pick_lang, t
from app.logs import get_logger

log = get_logger(__name__)


class ThrottleMiddleware(BaseMiddleware):
    def __init__(self, redis: Redis, limit: int, window_seconds: int) -> None:
        self.redis, self.limit, self.window = redis, limit, window_seconds

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, Update) or self.limit <= 0:
            return await handler(event, data)
        message, callback = event.message, event.callback_query
        chat = message.chat if message else (callback.message.chat if callback and callback.message else None)
        user = message.from_user if message else (callback.from_user if callback else None)
        if user is None or chat is None or chat.type != "private" or (message and message.media_group_id):
            return await handler(event, data)
        if message and (message.web_app_data or (message.text or "").startswith("/start ci_")):
            return await handler(event, data)  # скан QR на входе не режем: у неверных кодов свой лимит

        key = f"thr:{user.id}:{int(time.time() // self.window)}"
        try:
            count = await self.redis.incr(key)
            if count == 1:
                await self.redis.expire(key, self.window + 1)
        except Exception as exc:
            log.warning("throttle_redis_error", error=repr(exc))
            return await handler(event, data)
        if count <= self.limit:
            return await handler(event, data)

        if count == self.limit + 1:  # предупреждаем один раз за окно, дальше молчим
            log.info("throttled", user_id=user.id)
            text = t("errors.too_fast", pick_lang(user.language_code))
            bot: Bot = data["bot"]
            try:
                if callback:
                    await bot.answer_callback_query(callback.id, text)
                else:
                    await bot.send_message(chat.id, text)
            except Exception:
                pass
        return None
