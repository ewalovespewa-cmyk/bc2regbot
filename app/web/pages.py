"""Веб-страницы: экран чекина с меняющимся QR, дашборд, Mini App со сканером QR."""
import hmac
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import segno
from aiohttp import web
from sqlalchemy import func, select

from app.db.models import Checkin, EventDay, Participant, Team
from app.services import checkin_codes
from app.services.checkin import checkins_timeline, day_stats, is_open, today_day
from app.services.importer import team_display_name
from app.web.keys import SESSIONMAKER, SETTINGS

TEMPLATES = Path(__file__).parent / "templates"
NO_CACHE = {"Cache-Control": "no-store"}


def _check_key(request: web.Request, secret: str) -> None:
    key = request.query.get("key", "")
    if not secret or not hmac.compare_digest(key.encode(), secret.encode()):
        raise web.HTTPForbidden(text="forbidden")


def _template(name: str) -> str:
    return (TEMPLATES / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------- экран чекина


async def checkin_screen(request: web.Request) -> web.Response:
    _check_key(request, request.app[SETTINGS].screen_secret)
    return web.Response(text=_template("checkin_screen.html"), content_type="text/html", headers=NO_CACHE)


async def checkin_screen_state(request: web.Request) -> web.Response:
    settings = request.app[SETTINGS]
    _check_key(request, settings.screen_secret)
    async with request.app[SESSIONMAKER]() as session:
        day = await today_day(session, settings)
        total = await session.scalar(select(func.count()).select_from(Participant).where(Participant.is_selected)) or 0
        checked = 0
        if day is not None:
            checked = await session.scalar(select(func.count()).select_from(Checkin).where(Checkin.event_day_id == day.id)) or 0

    body: dict = {"checked": checked, "total": total, "day": day.label if day else None}
    if day is None or not is_open(day):
        body["state"] = "closed"
    elif not day.is_screen_enabled:
        body["state"] = "screen_off"
    elif not settings.checkin_secret or not settings.bot_username:
        body["state"] = "misconfigured"
    else:
        code, seconds_left = checkin_codes.current_code(settings.checkin_secret, day.id)
        link = checkin_codes.deep_link(settings.bot_username, code)
        body |= {
            "state": "open",
            "seconds_left": seconds_left,
            # omitsize: без width/height и с viewBox — SVG тянется на всю карточку, а не висит в углу.
            "svg": segno.make(link, error="m").svg_inline(border=2, dark="#0b0a12", light="#ffffff", omitsize=True),
        }
    return web.json_response(body, headers=NO_CACHE)


# ---------------------------------------------------------------- дашборд


async def dashboard(request: web.Request) -> web.Response:
    _check_key(request, request.app[SETTINGS].dashboard_secret)
    return web.Response(text=_template("dashboard.html"), content_type="text/html", headers=NO_CACHE)


async def dashboard_data(request: web.Request) -> web.Response:
    settings = request.app[SETTINGS]
    _check_key(request, settings.dashboard_secret)
    tz = ZoneInfo(settings.timezone)
    async with request.app[SESSIONMAKER]() as session:
        days = (await session.scalars(select(EventDay).order_by(EventDay.date))).all()
        requested = request.query.get("day")
        day = next((d for d in days if str(d.id) == requested), None) or await today_day(session, settings)
        day = day or (days[0] if days else None)
        total = await session.scalar(select(func.count()).select_from(Participant).where(Participant.is_selected)) or 0
        linked = await session.scalar(
            select(func.count()).select_from(Participant).where(Participant.is_selected, Participant.telegram_user_id.is_not(None))
        ) or 0
        payload: dict = {
            "generated_at": datetime.now(UTC).astimezone(tz).strftime("%H:%M:%S"),
            "days": [{"id": d.id, "label": d.label, "open": is_open(d)} for d in days],
            "total": total,
            "linked": linked,
        }
        if day is not None:
            stats = await day_stats(session, day)
            checked_ids = set(
                (await session.scalars(select(Checkin.participant_id).where(Checkin.event_day_id == day.id))).all()
            )
            rows = await session.execute(
                select(Team.name, Team.is_solo, Participant.full_name, Participant.id)
                .join(Participant, Participant.team_id == Team.id)
                .where(Participant.is_selected)
                .order_by(Team.name, Participant.full_name)
            )
            teams: dict[str, dict] = {}
            for name, is_solo, full_name, pid in rows:
                key = f"Соло · {full_name}" if is_solo else name
                team = teams.setdefault(key, {"name": key, "solo": is_solo, "present": 0, "total": 0, "members": []})
                team["total"] += 1
                team["present"] += pid in checked_ids
                team["members"].append({"name": full_name, "here": pid in checked_ids})
            recent = await session.execute(
                select(Checkin.created_at, Checkin.method, Participant.full_name, Participant.team_id)
                .join(Participant, Participant.id == Checkin.participant_id)
                .where(Checkin.event_day_id == day.id)
                .order_by(Checkin.created_at.desc())
                .limit(15)
            )
            team_names = {t.id: t for t in (await session.scalars(select(Team))).all()}
            payload |= {
                "day": {"id": day.id, "label": day.label, "open": is_open(day)},
                "checked": stats.checked,
                "teams_full": len(stats.teams_full),
                "teams_partial": len(stats.teams_partial),
                "teams_none": len(stats.teams_none),
                "timeline": [
                    {"t": datetime.fromisoformat(ts).astimezone(tz).strftime("%H:%M"), "n": n}
                    for ts, n in await checkins_timeline(session, day)
                ],
                "teams": sorted(teams.values(), key=lambda t: (t["solo"], t["present"] == t["total"], t["name"])),
                "recent": [
                    {
                        "time": at.astimezone(tz).strftime("%H:%M"),
                        "name": name,
                        "team": team_display_name(team_names.get(team_id)),
                        "method": method.value,
                    }
                    for at, method, name, team_id in recent
                ],
            }
    return web.json_response(payload, headers=NO_CACHE)


# ---------------------------------------------------------------- Mini App


async def webapp_scan(request: web.Request) -> web.Response:
    return web.Response(text=_template("webapp_scan.html"), content_type="text/html", headers=NO_CACHE)


def register(app: web.Application) -> None:
    app.router.add_get("/checkin-screen", checkin_screen)
    app.router.add_get("/checkin-screen/state", checkin_screen_state)
    app.router.add_get("/dashboard", dashboard)
    app.router.add_get("/dashboard/data", dashboard_data)
    app.router.add_get("/webapp/scan", webapp_scan)
