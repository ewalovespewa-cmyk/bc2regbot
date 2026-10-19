"""Типизированные ключи aiohttp-приложения (app[KEY])."""
from aiogram import Bot, Dispatcher
from aiohttp import web
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.config import Settings

SETTINGS = web.AppKey("settings", Settings)
ENGINE = web.AppKey("engine", AsyncEngine)
SESSIONMAKER = web.AppKey("sessionmaker", async_sessionmaker[AsyncSession])
REDIS = web.AppKey("redis", Redis)
BOT = web.AppKey("bot", Bot)
DISPATCHER = web.AppKey("dispatcher", Dispatcher)
