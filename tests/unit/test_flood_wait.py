"""429 от Telegram: ждём и повторяем, слишком долгую паузу не ждём."""
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramRetryAfter
from aiogram.methods import SendMessage

from app.bot import flood_wait
from app.bot.flood_wait import ATTEMPTS, MAX_WAIT_SECONDS, FloodWaitMiddleware

METHOD = SendMessage(chat_id=1, text="x")


def _flood(seconds: int) -> TelegramRetryAfter:
    return TelegramRetryAfter(method=METHOD, message="Too Many Requests", retry_after=seconds)


@pytest.fixture
def sleep(monkeypatch):
    mock = AsyncMock()
    monkeypatch.setattr(flood_wait.asyncio, "sleep", mock)
    return mock


async def test_retries_after_waiting(sleep):
    make_request = AsyncMock(side_effect=[_flood(5), "ok"])
    assert await FloodWaitMiddleware()(make_request, None, METHOD) == "ok"
    assert make_request.await_count == 2
    assert 4 < sleep.await_args.args[0] < 6


async def test_pause_is_shared_between_requests(sleep):
    middleware = FloodWaitMiddleware()
    await middleware(AsyncMock(side_effect=[_flood(5), "ok"]), None, METHOD)
    sleep.reset_mock()
    await middleware(AsyncMock(return_value="ok"), None, METHOD)
    assert sleep.await_count == 1  # следующий запрос тоже дождался конца паузы


async def test_too_long_pause_is_not_waited(sleep):
    make_request = AsyncMock(side_effect=_flood(MAX_WAIT_SECONDS + 1))
    with pytest.raises(TelegramRetryAfter):
        await FloodWaitMiddleware()(make_request, None, METHOD)
    assert make_request.await_count == 1 and sleep.await_count == 0


async def test_gives_up_after_attempts(sleep):
    make_request = AsyncMock(side_effect=_flood(1))
    with pytest.raises(TelegramRetryAfter):
        await FloodWaitMiddleware()(make_request, None, METHOD)
    assert make_request.await_count == ATTEMPTS
