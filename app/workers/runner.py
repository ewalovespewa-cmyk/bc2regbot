"""Фоновый планировщик периодических задач.

Задачи выполняет только процесс, держащий лидер-лок в Redis. При деплое Railway
ненадолго держит старый и новый контейнер одновременно — лок гарантирует, что
задача не выполнится дважды. Время последнего запуска тоже хранится в Redis,
поэтому рестарт не перезапускает всё сразу.
"""
import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from aiogram import Bot
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.logs import get_logger

log = get_logger(__name__)

LOCK_KEY = "workers:leader"
LOCK_TTL = 45
TICK_SECONDS = 2  # интервалы задач задаются отдельно; тик только проверяет, не пора ли

_RENEW = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('expire', KEYS[1], ARGV[2])
end
return 0
"""
_RELEASE = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('del', KEYS[1])
end
return 0
"""


@dataclass
class JobContext:
    bot: Bot
    redis: Redis
    sessionmaker: async_sessionmaker[AsyncSession]
    settings: Settings


@dataclass
class Job:
    name: str
    run: Callable[[JobContext], Awaitable[None]]
    interval: Callable[[JobContext], float]  # секунды; ≤ 0 — задача выключена
    timeout: float = 300


class LeaderLock:
    def __init__(self, redis: Redis, key: str = LOCK_KEY, ttl: int = LOCK_TTL) -> None:
        self.redis, self.key, self.ttl = redis, key, ttl
        self.token = uuid.uuid4().hex

    async def acquire_or_renew(self) -> bool:
        if await self.redis.eval(_RENEW, 1, self.key, self.token, self.ttl):
            return True
        return bool(await self.redis.set(self.key, self.token, nx=True, ex=self.ttl))

    async def release(self) -> None:
        await self.redis.eval(_RELEASE, 1, self.key, self.token)


class Scheduler:
    def __init__(self, ctx: JobContext, jobs: list[Job], *, tick: float = TICK_SECONDS) -> None:
        self.ctx, self.jobs, self.tick = ctx, jobs, tick
        self.lock = LeaderLock(ctx.redis)
        self._task: asyncio.Task | None = None
        self._heartbeat: asyncio.Task | None = None
        self.is_leader = False

    def start(self) -> None:
        self._heartbeat = asyncio.create_task(self._heartbeat_loop(), name="scheduler-heartbeat")
        self._task = asyncio.create_task(self._loop(), name="scheduler")

    async def stop(self) -> None:
        for task in (self._task, self._heartbeat):
            if task:
                task.cancel()
        for task in (self._task, self._heartbeat):
            if task:
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        try:
            await self.lock.release()
        except Exception:
            pass

    async def _heartbeat_loop(self) -> None:
        while True:
            try:
                was = self.is_leader
                self.is_leader = await self.lock.acquire_or_renew()
                if self.is_leader != was:
                    log.info("scheduler_leadership", leader=self.is_leader)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.is_leader = False
                log.warning("scheduler_lock_error", error=repr(exc))
            await asyncio.sleep(LOCK_TTL / 3)

    async def _loop(self) -> None:
        while True:
            if self.is_leader:
                for job in self.jobs:
                    if not self.is_leader:
                        break
                    await self._maybe_run(job)
            await asyncio.sleep(self.tick)

    async def _maybe_run(self, job: Job) -> None:
        try:
            interval = job.interval(self.ctx)
            if interval <= 0:
                return
            key = f"job:last:{job.name}"
            last = float(await self.ctx.redis.get(key) or 0)
            if time.time() - last < interval:
                return
            await self.ctx.redis.set(key, str(time.time()))
            started = time.monotonic()
            await asyncio.wait_for(job.run(self.ctx), job.timeout)
            seconds = round(time.monotonic() - started, 2)
            # Частые пустые проходы (рассылки раз в 2 с) не засоряют лог; долгие — видно.
            (log.info if seconds >= 1 else log.debug)("job_done", job=job.name, seconds=seconds)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("job_failed", job=job.name, exc_info=exc)
            await self._alert(job.name, exc)

    async def _alert(self, job_name: str, exc: BaseException) -> None:
        from app.bot.errors import send_error_alert

        try:
            await send_error_alert(self.ctx.bot, self.ctx.redis, self.ctx.settings, exc, None, None)
        except Exception:
            log.warning("job_alert_failed", job=job_name)
