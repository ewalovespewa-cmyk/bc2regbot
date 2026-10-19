"""/health против настоящего Postgres и проверка секрета webhook."""
from unittest.mock import AsyncMock

from aiogram import Bot
from aiogram.webhook.aiohttp_server import SimpleRequestHandler
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import text

from tests.harness import make_dispatcher as create_dispatcher
from app.config import Settings
from app.db.session import create_sessionmaker
from app.web.health import health
from app.web.keys import ENGINE, REDIS


async def _client(app: web.Application) -> TestClient:
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


async def test_migrations_created_all_tables(engine):
    async with engine.connect() as conn:
        rows = await conn.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))
        tables = {r[0] for r in rows}
    assert {"participants", "teams", "checkins", "broadcast_deliveries", "outbound_events"} <= tables


async def test_health_ok(engine, redis):
    app = web.Application()
    app[ENGINE], app[REDIS] = engine, redis
    app.router.add_get("/health", health)
    client = await _client(app)
    try:
        resp = await client.get("/health")
        assert resp.status == 200
        assert await resp.json() == {"status": "ok", "db": "ok", "redis": "ok"}
    finally:
        await client.close()


async def test_health_503_when_redis_down(engine):
    broken = AsyncMock()
    broken.ping.side_effect = ConnectionError("redis down")
    app = web.Application()
    app[ENGINE], app[REDIS] = engine, broken
    app.router.add_get("/health", health)
    client = await _client(app)
    try:
        resp = await client.get("/health")
        assert resp.status == 503
        assert (await resp.json())["redis"] == "fail"
    finally:
        await client.close()


async def test_webhook_rejects_wrong_secret(engine, redis):
    settings = Settings(_env_file=None)
    bot = Bot("123456:TEST-TOKEN")  # апдейт без сообщения не вызывает Bot API
    dp = create_dispatcher(bot=bot, redis=redis, sessionmaker=create_sessionmaker(engine), settings=settings)
    app = web.Application()
    SimpleRequestHandler(dispatcher=dp, bot=bot, secret_token="right", handle_in_background=False).register(
        app, path="/tg/webhook"
    )
    client = await _client(app)
    try:
        resp = await client.post(
            "/tg/webhook", json={"update_id": 1}, headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"}
        )
        assert resp.status == 401
        resp = await client.post(
            "/tg/webhook", json={"update_id": 1}, headers={"X-Telegram-Bot-Api-Secret-Token": "right"}
        )
        assert resp.status == 200
    finally:
        await client.close()
        await bot.session.close()
