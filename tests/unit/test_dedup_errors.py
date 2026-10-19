from unittest.mock import AsyncMock

import pytest
from aiogram.types import Update

from app.bot.errors import error_signature, format_alert, send_error_alert
from app.bot.middlewares.dedup import DedupMiddleware
from app.config import Settings


def make_update(update_id: int) -> Update:
    return Update(update_id=update_id)


async def test_same_update_processed_once(redis):
    mw = DedupMiddleware(redis, bot_id=1)
    handler = AsyncMock(return_value="ok")

    assert await mw(handler, make_update(10), {}) == "ok"
    assert await mw(handler, make_update(10), {}) is None
    assert await mw(handler, make_update(11), {}) == "ok"
    assert handler.await_count == 2
    assert await redis.get("upd:1:10") == "done"


async def test_failed_update_can_be_retried(redis):
    mw = DedupMiddleware(redis, bot_id=1)
    failing = AsyncMock(side_effect=RuntimeError("boom"))
    with pytest.raises(RuntimeError):
        await mw(failing, make_update(20), {})

    ok = AsyncMock(return_value="ok")
    assert await mw(ok, make_update(20), {}) == "ok"


async def test_concurrent_duplicate_skipped_while_processing(redis):
    mw = DedupMiddleware(redis, bot_id=1)
    await redis.set("upd:1:30", "processing", ex=60)
    handler = AsyncMock()
    assert await mw(handler, make_update(30), {}) is None
    handler.assert_not_awaited()


def _raise(exc: Exception) -> Exception:
    try:
        raise exc
    except Exception as caught:
        return caught


def test_error_signature_stable_for_same_place():
    sigs = {error_signature(_raise(ValueError(f"msg {i}"))) for i in range(3)}
    assert len(sigs) == 1
    assert error_signature(_raise(KeyError("x"))) not in sigs


def test_alert_fits_telegram_limit():
    exc = _raise(ValueError("x" * 10_000))
    text = format_alert(exc, update_id=1, user_id=2)
    assert len(text) <= 4096
    assert "&lt;" not in text[:50]  # заголовок не экранирован, экранирован только трейсбек


async def test_error_alert_throttled(redis, monkeypatch):
    monkeypatch.setenv("ADMIN_CHAT_ID", "-100500")
    settings = Settings(_env_file=None)
    bot = AsyncMock()
    exc = _raise(ValueError("boom"))

    assert await send_error_alert(bot, redis, settings, exc, 1, 2) is True
    assert await send_error_alert(bot, redis, settings, exc, 3, 4) is False
    assert bot.send_message.await_count == 1


async def test_no_alert_without_admin_chat(redis, monkeypatch):
    monkeypatch.delenv("ADMIN_CHAT_ID", raising=False)
    bot = AsyncMock()
    assert await send_error_alert(bot, redis, Settings(_env_file=None), _raise(ValueError()), 1, 2) is False
    bot.send_message.assert_not_awaited()
