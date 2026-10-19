"""Дедупликация апдейтов по update_id.

Telegram повторяет доставку, если не получил 200 (таймаут, рестарт контейнера).
Ключ ставится в два шага: "processing" с коротким TTL на время обработки, затем
"done" на 48 часов. Если процесс упал посреди обработки, ключ "processing" истечёт
и следующий повтор от Telegram будет обработан. Повтор, пришедший параллельно с
обработкой или после неё, отбрасывается.
"""
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Update
from redis.asyncio import Redis

from app.logs import get_logger

log = get_logger(__name__)

PROCESSING_TTL = 120
DONE_TTL = 48 * 3600


class DedupMiddleware(BaseMiddleware):
    def __init__(self, redis: Redis, bot_id: int) -> None:
        self.redis = redis
        self.bot_id = bot_id

    def key(self, update_id: int) -> str:
        return f"upd:{self.bot_id}:{update_id}"

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, Update):
            return await handler(event, data)

        key = self.key(event.update_id)
        if not await self.redis.set(key, "processing", nx=True, ex=PROCESSING_TTL):
            log.info("duplicate_update_skipped", update_id=event.update_id)
            return None
        try:
            result = await handler(event, data)
        except BaseException:
            # Снимаем ключ. Если errors-router обработает ошибку, Telegram получит 200 и повтора
            # не будет; если нет — вернётся 500 и повтор обработается заново.
            await self.redis.delete(key)
            raise
        await self.redis.set(key, "done", ex=DONE_TTL)
        return result
