from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.fsm.storage.redis import DefaultKeyBuilder, RedisStorage
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot import errors
from app.bot.handlers import service, start
from app.bot.middlewares.db_session import DbSessionMiddleware
from app.bot.middlewares.dedup import DedupMiddleware
from app.config import Settings

TELEGRAM_TIMEOUT = 30  # секунд на любой запрос к Bot API


def create_bot(settings: Settings) -> Bot:
    return Bot(
        settings.bot_token,
        session=AiohttpSession(timeout=TELEGRAM_TIMEOUT),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True),
    )


def create_dispatcher(
    *,
    bot: Bot,
    redis: Redis,
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> Dispatcher:
    storage = RedisStorage(redis, key_builder=DefaultKeyBuilder(with_bot_id=True), state_ttl=7 * 86400, data_ttl=7 * 86400)
    dp = Dispatcher(storage=storage, redis=redis, sessionmaker=sessionmaker, settings=settings)

    # Порядок важен: сначала отбрасываем дубли, потом открываем сессию БД.
    dp.update.outer_middleware(DedupMiddleware(redis, bot.id))
    dp.update.outer_middleware(DbSessionMiddleware(sessionmaker))

    dp.include_routers(errors.router, service.router, start.router)
    return dp
