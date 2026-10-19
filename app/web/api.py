"""Внутреннее API для бота игр. Авторизация: заголовок Authorization: Bearer <INTEGRATION_TOKEN>."""
import hmac
from datetime import date

from aiohttp import web
from sqlalchemy import select

from app.db.models import Checkin, EventDay, Participant
from app.services.checkin import local_today
from app.services.importer import team_display_name
from app.web.keys import SESSIONMAKER, SETTINGS


def _authorize(request: web.Request) -> None:
    token = request.app[SETTINGS].integration_token
    header = request.headers.get("Authorization", "")
    given = header.removeprefix("Bearer ").strip()
    if not token or not hmac.compare_digest(given.encode(), token.encode()):
        raise web.HTTPUnauthorized(text="unauthorized")


def _user_id(request: web.Request) -> int:
    try:
        return int(request.query["telegram_user_id"])
    except (KeyError, ValueError):
        raise web.HTTPBadRequest(text="telegram_user_id is required") from None


async def participant_status(request: web.Request) -> web.Response:
    """Статус по Telegram ID: активировал ли бота участника, команда, чекины по дням."""
    _authorize(request)
    settings = request.app[SETTINGS]
    user_id = _user_id(request)
    async with request.app[SESSIONMAKER]() as session:
        p = await session.scalar(
            select(Participant).where(Participant.telegram_user_id == user_id, Participant.is_selected)
        )
        if p is None:
            return web.json_response({"telegram_user_id": user_id, "activated": False, "checkins": [], "checked_in_today": False})
        rows = await session.execute(
            select(EventDay.date, Checkin.created_at)
            .join(Checkin, Checkin.event_day_id == EventDay.id)
            .where(Checkin.participant_id == p.id)
            .order_by(EventDay.date)
        )
        checkins = [{"day": d.isoformat(), "at": at.isoformat()} for d, at in rows]
    today = local_today(settings).isoformat()
    return web.json_response(
        {
            "telegram_user_id": user_id,
            "activated": True,
            "participant_id": p.id,
            "full_name": p.full_name,
            "team": team_display_name(p.team, "en"),
            "checkins": checkins,
            "checked_in_today": any(c["day"] == today for c in checkins),
        }
    )


async def checkin_status(request: web.Request) -> web.Response:
    """GET /api/checkin-status?telegram_user_id=…&day=YYYY-MM-DD (по умолчанию — сегодня)."""
    _authorize(request)
    settings = request.app[SETTINGS]
    user_id = _user_id(request)
    try:
        day = date.fromisoformat(request.query.get("day") or local_today(settings).isoformat())
    except ValueError:
        raise web.HTTPBadRequest(text="day must be YYYY-MM-DD") from None
    async with request.app[SESSIONMAKER]() as session:
        at = await session.scalar(
            select(Checkin.created_at)
            .join(EventDay, EventDay.id == Checkin.event_day_id)
            .join(Participant, Participant.id == Checkin.participant_id)
            .where(Participant.telegram_user_id == user_id, Participant.is_selected, EventDay.date == day)
        )
    return web.json_response(
        {"telegram_user_id": user_id, "day": day.isoformat(), "checked_in": at is not None, "at": at.isoformat() if at else None}
    )


def register(app: web.Application) -> None:
    app.router.add_get("/api/participant-status", participant_status)
    app.router.add_get("/api/checkin-status", checkin_status)
