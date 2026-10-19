"""GET /health — Railway healthcheck. 200, если живы и БД, и Redis; иначе 503."""
import asyncio

from aiohttp import web
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.logs import get_logger
from app.web.keys import ENGINE, REDIS

log = get_logger(__name__)
CHECK_TIMEOUT = 2.0


async def _check_db(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))


async def _check_redis(redis: Redis) -> None:
    await redis.ping()


async def _run(name: str, coro) -> tuple[str, str]:
    try:
        await asyncio.wait_for(coro, CHECK_TIMEOUT)
        return name, "ok"
    except Exception as exc:
        log.warning("health_check_failed", component=name, error=repr(exc))
        return name, "fail"


async def health(request: web.Request) -> web.Response:
    results = dict(
        await asyncio.gather(
            _run("db", _check_db(request.app[ENGINE])),
            _run("redis", _check_redis(request.app[REDIS])),
        )
    )
    ok = all(v == "ok" for v in results.values())
    return web.json_response({"status": "ok" if ok else "fail", **results}, status=200 if ok else 503)
