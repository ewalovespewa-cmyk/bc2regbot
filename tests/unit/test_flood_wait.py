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


def _network_error(cause: Exception):
    from aiogram.exceptions import TelegramNetworkError

    try:
        raise TelegramNetworkError(method=METHOD, message="Cannot connect") from cause
    except TelegramNetworkError as exc:
        return exc


async def test_connection_reset_before_sending_is_retried(sleep):
    from aiohttp import ClientConnectorError
    from aiohttp.client_reqrep import ConnectionKey

    key = ConnectionKey("api.telegram.org", 443, True, True, None, None, None)
    not_connected = _network_error(ClientConnectorError(key, ConnectionResetError(104, "Connection reset by peer")))
    make_request = AsyncMock(side_effect=[not_connected, "ok"])
    assert await FloodWaitMiddleware()(make_request, None, METHOD) == "ok"
    assert make_request.await_count == 2


async def test_error_after_sending_is_not_retried(sleep):
    from aiogram.exceptions import TelegramNetworkError
    from aiohttp import ServerDisconnectedError

    make_request = AsyncMock(side_effect=_network_error(ServerDisconnectedError()))
    with pytest.raises(TelegramNetworkError):
        await FloodWaitMiddleware()(make_request, None, METHOD)
    assert make_request.await_count == 1  # сообщение могло дойти — не дублируем
