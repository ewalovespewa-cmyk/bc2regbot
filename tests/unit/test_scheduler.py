import asyncio
from unittest.mock import AsyncMock, MagicMock

from app.workers.runner import Job, JobContext, LeaderLock, Scheduler


async def test_only_one_leader(redis):
    a, b = LeaderLock(redis, ttl=30), LeaderLock(redis, ttl=30)
    assert await a.acquire_or_renew() is True
    assert await b.acquire_or_renew() is False
    assert await a.acquire_or_renew() is True  # продление своего лока

    await a.release()
    assert await b.acquire_or_renew() is True
    await a.release()  # чужой лок не снимается
    assert await redis.get(b.key) == b.token


def _ctx(redis) -> JobContext:
    return JobContext(bot=AsyncMock(), redis=redis, sessionmaker=MagicMock(), settings=MagicMock(admin_chat_id=None))


async def test_job_respects_interval_across_restarts(redis):
    calls = []

    async def work(ctx):
        calls.append(1)

    job = Job("demo", work, lambda ctx: 3600)
    first = Scheduler(_ctx(redis), [job])
    await first._maybe_run(job)
    await first._maybe_run(job)
    # «Рестарт»: новый планировщик видит время последнего запуска в Redis.
    await Scheduler(_ctx(redis), [job])._maybe_run(job)
    assert len(calls) == 1


async def test_disabled_and_failing_jobs_do_not_break_loop(redis):
    async def boom(ctx):
        raise RuntimeError("sheets down")

    ran = []

    async def ok(ctx):
        ran.append(1)

    sched = Scheduler(_ctx(redis), [Job("off", ok, lambda ctx: 0), Job("boom", boom, lambda ctx: 1), Job("ok", ok, lambda ctx: 1)])
    for job in sched.jobs:
        await sched._maybe_run(job)
    assert ran == [1]


async def test_scheduler_runs_jobs_when_leader(redis):
    done = asyncio.Event()

    async def work(ctx):
        done.set()

    sched = Scheduler(_ctx(redis), [Job("w", work, lambda ctx: 1)], tick=0.01)
    sched.start()
    try:
        await asyncio.wait_for(done.wait(), 2)
    finally:
        await sched.stop()
    assert await redis.get("workers:leader") is None  # лок отпущен при остановке


async def test_missing_sheet_tab_gives_friendly_throttled_alert(redis, monkeypatch):
    from app.services.sources import SheetMissing
    from app.workers import jobs

    async def missing(session, settings):
        raise SheetMissing("В Google-таблице нет вкладки «Прошли отбор».")

    async def enabled(session, key, default=None):
        return True

    session = AsyncMock()
    ctx = JobContext(bot=AsyncMock(), redis=redis, sessionmaker=MagicMock(return_value=session),
                     settings=MagicMock(admin_chat_id=-100))
    session.__aenter__.return_value = session
    monkeypatch.setattr(jobs, "import_from_sheet", missing)
    monkeypatch.setattr(jobs, "get_setting", enabled)

    await jobs.auto_import(ctx)  # не бросает исключение
    await jobs.auto_import(ctx)
    sent = ctx.bot.send_message.await_args_list
    assert len(sent) == 1  # второй раз — тишина (раз в час)
    assert "нет вкладки «Прошли отбор»" in sent[0].args[1] and "Traceback" not in sent[0].args[1]
