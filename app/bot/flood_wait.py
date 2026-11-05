"""Ответ 429 от Telegram: все запросы бота ждут retry_after и повторяются.

Без этого 429 на ответ участнику превращался в ошибку обработчика. Пауза общая: пока Telegram просит
подождать, новые запросы тоже ждут и не продлевают ограничение. Слишком долгую паузу не ждём, чтобы
не держать обработку апдейта: исключение уходит вызывающему коду.
"""
import asyncio
import random
import time
from typing import Any

from aiogram import Bot
from aiogram.client.session.middlewares.base import BaseRequestMiddleware, NextRequestMiddlewareType
from aiogram.exceptions import TelegramRetryAfter
from aiogram.methods import TelegramMethod

from app.logs import get_logger

log = get_logger(__name__)

MAX_WAIT_SECONDS = 20
ATTEMPTS = 3


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
        raise AssertionError("unreachable")
