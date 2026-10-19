"""Общие фикстуры.

Интеграционные тесты идут против настоящего Postgres:
- если задан TEST_DATABASE_URL — используется он (например, dev-база на Railway);
- иначе поднимается встроенный Postgres из пакета pgserver (данные в .pgdata/, нужен один раз).
Redis в тестах — fakeredis.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fakeredis import FakeAsyncRedis

import app.db.models  # noqa: F401  все таблицы в metadata

ROOT = Path(__file__).resolve().parent.parent

# Settings требуют обязательные переменные; в тестах подставляем безопасные значения.
os.environ.setdefault("BOT_TOKEN", "123456:TEST-TOKEN")
os.environ.setdefault("WEBHOOK_SECRET", "test-secret")
os.environ.setdefault("DATABASE_URL", "postgresql://postgres:@127.0.0.1:5432/unused")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")


def _local_pg_url() -> str:
    pgserver = pytest.importorskip("pgserver", reason="нет TEST_DATABASE_URL и не установлен pgserver")
    server = pgserver.get_server(str(ROOT / ".pgdata"), cleanup_mode=None)
    server.psql("DROP DATABASE IF EXISTS bc_test WITH (FORCE);")
    server.psql("CREATE DATABASE bc_test;")
    base = server.get_uri()  # postgresql://postgres:@host:port/postgres
    return base.rsplit("/", 1)[0] + "/bc_test"


@pytest.fixture(scope="session")
def pg_url() -> str:
    url = os.environ.get("TEST_DATABASE_URL") or _local_pg_url()
    # Схема создаётся настоящими миграциями: заодно проверяем, что они применяются с нуля.
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT,
        env={**os.environ, "DATABASE_URL": url},
        check=True,
        capture_output=True,
    )
    return url


@pytest.fixture(scope="session")
async def engine(pg_url):
    from app.config import normalize_db_url
    from app.db.session import create_engine

    eng = create_engine(normalize_db_url(pg_url))
    yield eng
    await eng.dispose()


@pytest.fixture
async def session(engine):
    """Сессия на чистой БД: перед каждым тестом все таблицы очищаются."""
    from sqlalchemy import text

    from app.db.base import Base
    from app.db.session import create_sessionmaker

    tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    async with create_sessionmaker(engine)() as s:
        yield s


@pytest.fixture
async def redis():
    client = FakeAsyncRedis(decode_responses=True)
    yield client
    await client.flushall()
    await client.aclose()
