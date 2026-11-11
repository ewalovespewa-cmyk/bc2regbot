"""Ответ 429 от Telegram и обрывы связи: запросы бота ждут и повторяются.

Без этого 429 на ответ участнику превращался в ошибку обработчика. Пауза общая: пока Telegram просит
подождать, новые запросы тоже ждут и не продлевают ограничение. Слишком долгую паузу не ждём, чтобы
не держать обработку апдейта: исключение уходит вызывающему коду.

Если до api.telegram.org не удалось даже подключиться (обрыв на стороне сети), запрос точно не ушёл,
и его безопасно повторить через полсекунды. Обрывы после отправки не повторяем: сообщение могло дойти.
"""
import asyncio
import random
import time
from typing import Any

from aiogram import Bot
from aiogram.client.session.middlewares.base import BaseRequestMiddleware, NextRequestMiddlewareType
from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter
from aiogram.methods import TelegramMethod
from aiohttp import ClientConnectorError

from app.logs import get_logger

log = get_logger(__name__)

MAX_WAIT_SECONDS = 20
ATTEMPTS = 3
CONNECT_RETRY_DELAY = 0.5  # секунды, растёт с каждой попыткой


def _not_sent(exc: TelegramNetworkError) -> bool:
    """Соединение не установилось — запрос до Telegram не дошёл."""
    return isinstance(exc.__cause__, ClientConnectorError)


class FloodWaitMiddleware(BaseRequestMiddleware):
    def __init__(self) -> None:
        self._resume_at = 0.0

    async def __call__(self, make_request: NextRequestMiddlewareType[Any], bot: Bot, method: TelegramMethod[Any]) -> Any:
        for attempt in range(1, ATTEMPTS + 1):
            wait = self._resume_at - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait + random.uniform(0, 0.5))  # не просыпаться всем в один момент
            try:
                return await make_request(bot, method)
            except TelegramRetryAfter as exc:
                if attempt == ATTEMPTS or exc.retry_after > MAX_WAIT_SECONDS:
                    raise
                self._resume_at = max(self._resume_at, time.monotonic() + exc.retry_after)
                log.warning("telegram_flood_wait", retry_after=exc.retry_after, method=type(method).__name__, attempt=attempt)
            except TelegramNetworkError as exc:
                if attempt == ATTEMPTS or not _not_sent(exc):
                    raise
                log.warning("telegram_connect_retry", method=type(method).__name__, attempt=attempt, error=str(exc)[:200])
                await asyncio.sleep(CONNECT_RETRY_DELAY * attempt)
        raise AssertionError("unreachable")
