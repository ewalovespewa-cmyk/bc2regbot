"""Нагрузочный прогон: 300 чекинов за N секунд и рассылка на 300 человек — без дублей и потерь.

Всё идёт через настоящий диспетчер, Postgres и движок рассылок; Telegram заменён
поддельным API (scripts/fake_telegram.py), который в фазе рассылки отвечает 429 на каждый
40-й запрос и 403 для пяти «заблокировавших бота».

    python -m scripts.load_test                    # 300 чекинов за 60 с (по умолчанию)
    python -m scripts.load_test --duration 600     # «честные» 10 минут
    LOAD_DATABASE_URL=postgresql://… python -m scripts.load_test   # своя база (будет ОЧИЩЕНА)

Без LOAD_DATABASE_URL поднимается встроенный Postgres (pgserver) с базой bc_load.
Redis — fakeredis, если не задан LOAD_REDIS_URL.
"""
import argparse
import asyncio
import os
import random
import subprocess
import sys
import time
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("BOT_TOKEN", "123456:LOAD-TEST")
os.environ.setdefault("WEBHOOK_SECRET", "load")
os.environ.setdefault("DATABASE_URL", "postgresql://unused")
os.environ.setdefault("REDIS_URL", "redis://unused")

FAKE_PORT = 8089
CHECKIN_SECRET = "load-test-secret"


def database_url() -> str:
    if url := os.environ.get("LOAD_DATABASE_URL"):
        return url
    import pgserver

    server = pgserver.get_server(str(ROOT / ".pgdata"), cleanup_mode=None)
    server.psql("DROP DATABASE IF EXISTS bc_load WITH (FORCE);")
    server.psql("CREATE DATABASE bc_load;")
    return server.get_uri().rsplit("/", 1)[0] + "/bc_load"


async def run(duration: float) -> bool:
    from aiogram import Bot
    from aiogram.client.session.aiohttp import AiohttpSession
    from aiogram.client.telegram import TelegramAPIServer
    from aiogram.types import Chat, Message, MessageEntity, Update, User
    from sqlalchemy import func, select, text

    from app.bot.setup import create_dispatcher
    from app.config import Settings, normalize_db_url
    from app.db.base import Base
    from app.db.models import Broadcast, BroadcastDelivery, BroadcastStatus, Checkin, EventDay, Participant
    from app.db.session import create_engine, create_sessionmaker
    from app.services import broadcast as engine_mod
    from app.services import checkin_codes
    from scripts import fake_telegram
    from scripts.seed import TG_BASE, build_rows

    url = database_url()
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=ROOT,
                   env={**os.environ, "DATABASE_URL": url}, check=True, capture_output=True)
    settings = Settings(_env_file=None, database_url=url, checkin_secret=CHECKIN_SECRET, bot_username="load_test_bot",
                        superadmin_ids=frozenset({1}), admin_chat_id=None, test_mode=False)
    engine = create_engine(normalize_db_url(url))
    sm = create_sessionmaker(engine)

    if redis_url := os.environ.get("LOAD_REDIS_URL"):
        from app.redis import create_redis

        redis = create_redis(redis_url)
        await redis.flushdb()
    else:
        from fakeredis import FakeAsyncRedis

        redis = FakeAsyncRedis(decode_responses=True)

    # --- данные: 300 привязанных участников, сегодняшний день с открытым чекином
    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {', '.join(t.name for t in Base.metadata.sorted_tables)} RESTART IDENTITY CASCADE"))
    from scripts.seed import seed_db  # noqa: F401  (импорт данных — тот же код, что у сидинга)
    from app.services.sheet_sync import import_rows

    async with sm() as session:
        await import_rows(session, settings, build_rows())
        for pid in (await session.scalars(select(Participant.id))).all():
            p = await session.get(Participant, pid)
            p.telegram_user_id = TG_BASE + pid
        now = datetime.now(UTC)
        session.add(EventDay(date=now.astimezone(ZoneInfo(settings.timezone)).date(), label="День 1",
                             checkin_opens_at=now - timedelta(hours=1), checkin_closes_at=now + timedelta(hours=2),
                             is_checkin_enabled=True))
        await session.commit()
        users = list((await session.scalars(select(Participant.telegram_user_id))).all())
        day_id = await session.scalar(select(EventDay.id))

    fake, runner = await fake_telegram.start(FAKE_PORT)
    bot = Bot(settings.bot_token, session=AiohttpSession(api=TelegramAPIServer.from_base(f"http://127.0.0.1:{FAKE_PORT}")))
    dp = create_dispatcher(bot=bot, redis=redis, sessionmaker=sm, settings=settings)
    ok = True

    # --- фаза 1: чекины
    print(f"▶ Фаза 1: {len(users)} чекинов за {duration:.0f} с (10% — повтор того же апдейта, 10% — второй скан)")
    update_ids = iter(range(1, 10**9))

    def start_update(uid: int, update_id: int) -> Update:
        code, _ = checkin_codes.current_code(CHECKIN_SECRET, day_id)
        payload = f"/start ci_{code}"
        msg = Message(message_id=update_id, date=datetime.now(UTC), chat=Chat(id=uid, type="private"),
                      from_user=User(id=uid, is_bot=False, first_name="Load", language_code="ru"),
                      text=payload, entities=[MessageEntity(type="bot_command", offset=0, length=6)])
        return Update(update_id=update_id, message=msg)

    rnd = random.Random(42)

    async def participant_flow(uid: int) -> None:
        await asyncio.sleep(rnd.uniform(0, duration))
        uid_update = next(update_ids)
        tasks = [dp.feed_update(bot, start_update(uid, uid_update))]
        roll = rnd.random()
        if roll < 0.1:  # Telegram повторно доставил тот же апдейт
            tasks.append(dp.feed_update(bot, start_update(uid, uid_update)))
        await asyncio.gather(*tasks)
        if 0.1 <= roll < 0.2:  # человек отсканировал ещё раз
            await asyncio.sleep(rnd.uniform(0.5, 3))
            await dp.feed_update(bot, start_update(uid, next(update_ids)))

    started = time.monotonic()
    await asyncio.gather(*(participant_flow(uid) for uid in users))
    phase1 = time.monotonic() - started

    async with sm() as session:
        checkins = await session.scalar(select(func.count()).select_from(Checkin))
        distinct = await session.scalar(select(func.count(func.distinct(Checkin.participant_id))))
    ok_messages = Counter(uid for uid, texts in fake.sent.items() for t in texts if "Ты отмечен" in t)
    doubled = [uid for uid, n in ok_messages.items() if n > 1]
    missing = [uid for uid in users if ok_messages[uid] == 0]
    print(f"  чекинов в БД: {checkins} (уникальных {distinct}) за {phase1:.1f} с")
    print(f"  «Ты отмечен» получили: {len(ok_messages)}; дважды: {len(doubled)}; без ответа: {len(missing)}")
    ok &= checkins == distinct == len(users) and not doubled and not missing

    # --- фаза 2: рассылка на всех с 429 и 403
    blocked = set(users[:5])
    fake.every_429, fake.blocked, fake.retry_after = 40, blocked, 1
    fake.sent.clear()
    print(f"▶ Фаза 2: рассылка на {len(users)} (429 на каждый 40-й запрос, {len(blocked)} заблокировали бота)")
    engine_mod.limiter = engine_mod.RateLimiter(engine_mod.SEND_RATE)
    async with sm() as session:
        b = Broadcast(title="Нагрузка", text_html="Нагрузочная рассылка, {name}!", segment={"type": "all"}, created_by=1)
        session.add(b)
        await session.flush()
        await engine_mod.start_broadcast(session, b, settings)
        await session.commit()
        bid = b.id

    started = time.monotonic()
    while time.monotonic() - started < 300:
        await engine_mod.run_worker_cycle(bot, sm, settings, time_budget=30)
        async with sm() as session:
            status = await session.scalar(select(Broadcast.status).where(Broadcast.id == bid))
            pending = await session.scalar(select(func.count()).select_from(BroadcastDelivery).where(
                BroadcastDelivery.broadcast_id == bid, BroadcastDelivery.status == "pending"))
        if status == BroadcastStatus.DONE:
            break
        if pending:
            await asyncio.sleep(1)
    phase2 = time.monotonic() - started

    async with sm() as session:
        stats = await engine_mod.delivery_stats(session, bid)
    got = Counter(uid for uid, texts in fake.sent.items() for t in texts if "Нагрузочная рассылка" in t)
    doubled = [uid for uid, n in got.items() if n > 1]
    missing = [uid for uid in users if uid not in blocked and got[uid] == 0]
    rate = stats["sent"] / phase2 if phase2 else 0
    print(f"  статус: {status.value}; доставлено {stats['sent']}, заблокировали {stats['blocked']}, ошибок {stats['failed']}, "
          f"в очереди {stats['pending']}")
    print(f"  429 получено: {fake.errors_429}; время: {phase2:.1f} с (~{rate:.1f} msg/s, лимит {engine_mod.SEND_RATE})")
    print(f"  дублей: {len(doubled)}; потерь: {len(missing)}")
    ok &= (status == BroadcastStatus.DONE and stats["sent"] == len(users) - len(blocked)
           and stats["blocked"] == len(blocked) and not doubled and not missing)

    await bot.session.close()
    await runner.cleanup()
    await engine.dispose()
    print("✅ Нагрузочный прогон пройден" if ok else "❌ Нагрузочный прогон НЕ пройден")
    return ok


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration", type=float, default=60, help="за сколько секунд прийти 300 участникам")
    args = parser.parse_args()
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    sys.exit(0 if asyncio.run(run(args.duration)) else 1)


if __name__ == "__main__":
    main()
