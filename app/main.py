"""Точка входа: aiohttp-сервер с webhook Telegram, /health и (позже) веб-страницами.

Если WEBHOOK_BASE_URL не задан (локальная разработка), бот работает через polling
в том же процессе. Используй для этого отдельного dev-бота: polling снимает webhook.
"""
import asyncio
import contextlib

from aiogram import Bot, Dispatcher
from aiogram.webhook.aiohttp_server import SimpleRequestHandler
from aiohttp import web

from app.bot.setup import create_bot, create_dispatcher
from app.config import Settings, get_settings
from app.db.session import create_engine, create_sessionmaker
from app.logs import get_logger, setup_logging
from app.redis import create_redis
from app.web.health import health
from app.web.keys import BOT, DISPATCHER, ENGINE, REDIS, SESSIONMAKER, SETTINGS

log = get_logger(__name__)


def build_app(settings: Settings) -> web.Application:
    engine = create_engine(settings.database_url)
    sessionmaker = create_sessionmaker(engine)
    redis = create_redis(settings.redis_url)
    bot = create_bot(settings)
    dp = create_dispatcher(bot=bot, redis=redis, sessionmaker=sessionmaker, settings=settings)

    app = web.Application()
    app[SETTINGS] = settings
    app[ENGINE] = engine
    app[SESSIONMAKER] = sessionmaker
    app[REDIS] = redis
    app[BOT] = bot
    app[DISPATCHER] = dp

    app.router.add_get("/health", health)
    SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
        secret_token=settings.webhook_secret,
        # Обрабатываем до ответа Telegram: если упадём посреди апдейта, Telegram его повторит.
        handle_in_background=False,
    ).register(app, path=settings.webhook_path)

    app.cleanup_ctx.append(_lifecycle)
    return app


async def _lifecycle(app: web.Application):
    settings, bot, dp = app[SETTINGS], app[BOT], app[DISPATCHER]
    me = await bot.get_me()
    if not settings.bot_username:
        settings.bot_username = me.username or ""
    log.info("bot_started", bot=me.username, test_mode=settings.test_mode)

    polling_task: asyncio.Task | None = None
    if settings.webhook_url:
        await bot.set_webhook(
            settings.webhook_url,
            secret_token=settings.webhook_secret,
            allowed_updates=dp.resolve_used_update_types(),
            drop_pending_updates=False,
        )
        log.info("webhook_set", url=settings.webhook_url)
    else:
        log.warning("polling_mode", reason="WEBHOOK_BASE_URL is empty")
        await bot.delete_webhook(drop_pending_updates=False)
        polling_task = asyncio.create_task(
            dp.start_polling(bot, handle_signals=False, allowed_updates=dp.resolve_used_update_types())
        )

    yield

    # Webhook при остановке не снимаем: при деплое новый контейнер уже поставил свой.
    if polling_task:
        await dp.stop_polling()
        with contextlib.suppress(asyncio.CancelledError):
            await polling_task
    await bot.session.close()
    await app[REDIS].aclose()
    await app[ENGINE].dispose()
    log.info("bot_stopped")


def main() -> None:
    settings = get_settings()
    setup_logging(settings.log_level, settings.log_json)
    web.run_app(build_app(settings), host="0.0.0.0", port=settings.port, print=None, access_log=None)


if __name__ == "__main__":
    main()
