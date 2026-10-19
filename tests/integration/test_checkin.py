"""Этап 7: самочекин по QR, отложенный чекин, волонтёр, отмена, экран и дашборд."""
import time
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from aiogram.methods import SendMessage
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import func, select

from app.config import Settings
from app.db.models import Admin, Checkin, CheckinMethod, EventDay, OutboundEvent, Participant, Role, Team
from app.db.session import create_sessionmaker
from app.services import checkin_codes
from app.services.checkin import day_stats
from app.web import pages
from app.web.keys import SESSIONMAKER, SETTINGS
from tests.harness import Harness

SUPER, VOL, ADMIN_ID = 900, 902, 901
ARU, DANA, NEW = 1001, 1002, 1005
SECRET = "test-checkin-secret"


@pytest.fixture
async def world(session):
    team = Team(name="Rockets")
    session.add(team)
    await session.flush()
    session.add_all(
        [
            Participant(email="aru@x.kz", full_name="Aru Bekova", phone="+77011112233", invite_token="ARUtoken0001",
                        team_id=team.id, telegram_user_id=ARU),
            Participant(email="dana@x.kz", full_name="Dana Ospan", phone="+77022223344", invite_token="DANAtoken001",
                        team_id=team.id, telegram_user_id=DANA),
            Participant(email="new@x.kz", full_name="New Person", phone="+77055556677", invite_token="NEWtoken0001",
                        team_id=team.id),
            Admin(telegram_user_id=VOL, role=Role.VOLUNTEER),
            Admin(telegram_user_id=ADMIN_ID, role=Role.ADMIN),
        ]
    )
    today = datetime.now(UTC).astimezone(ZoneInfo("Asia/Almaty")).date()
    now = datetime.now(UTC)
    day = EventDay(date=today, label="День 1", checkin_opens_at=now - timedelta(hours=1),
                   checkin_closes_at=now + timedelta(hours=1), is_checkin_enabled=True)
    session.add(day)
    await session.commit()
    return day


@pytest.fixture
def settings():
    return Settings(_env_file=None, superadmin_ids=frozenset({SUPER}), admin_chat_id=-100, checkin_secret=SECRET,
                    screen_secret="screen-key", dashboard_secret="dash-key", bot_username="bc_test_bot",
                    games_bot_username="bc_games_bot")


@pytest.fixture
def h(engine, redis, world, settings) -> Harness:
    return Harness(redis=redis, sessionmaker=create_sessionmaker(engine), settings=settings)


def code_for(day_id: int, offset: float = 0) -> str:
    return checkin_codes.current_code(SECRET, day_id, time.time() + offset)[0]


async def test_self_checkin_ok_already_and_team_count(h, session, world):
    await h.text(ARU, f"/start ci_{code_for(world.id)}")
    texts = h.session.texts(ARU)
    assert "Ты отмечен" in texts[-2] and "1/3 на месте" in texts[-2] and "Бинго открыто" in texts[-1]

    await h.text(DANA, f"/start ci_{code_for(world.id)}")
    assert "2/3 на месте" in h.session.texts(DANA)[-2]

    await h.text(ARU, f"/start ci_{code_for(world.id)}")
    assert "уже отмечен" in h.session.last_text(ARU)
    assert await session.scalar(select(func.count()).select_from(Checkin)) == 2
    events = (await session.scalars(select(OutboundEvent).where(OutboundEvent.type == "checkin.created"))).all()
    assert len(events) == 2 and events[0].payload["telegram_user_id"] == ARU


async def test_expired_code_and_closed_day(h, session, world):
    await h.text(ARU, f"/start ci_{code_for(world.id, offset=-90)}")
    assert "QR устарел" in h.session.last_text(ARU)

    world.is_checkin_enabled = False
    await session.commit()
    await h.text(ARU, f"/start ci_{code_for(world.id)}")
    assert "закрыт" in h.session.last_text(ARU)
    assert await session.scalar(select(func.count()).select_from(Checkin)) == 0


async def test_invalid_code_flood_is_limited_and_reported(h, session, world):
    for _ in range(11):
        await h.text(ARU, "/start ci_WRONGCODE1")
    assert "Слишком много попыток" in h.session.last_text(ARU)
    assert "неверным QR" in h.session.last_text(-100)


async def test_pending_checkin_after_linking_by_contact(h, session, world):
    await h.text(NEW, f"/start ci_{code_for(world.id)}")
    assert "Сначала привяжем аккаунт" in h.session.last_text(NEW)
    await h.contact(NEW, "87055556677")
    assert any("Ты отмечен" in t for t in h.session.texts(NEW))
    pid = await session.scalar(select(Participant.id).where(Participant.email == "new@x.kz"))
    assert await session.scalar(select(func.count()).select_from(Checkin).where(Checkin.participant_id == pid)) == 1


async def test_phone_mismatch_at_entrance_offers_help(h, session, world):
    await h.text(4444, f"/start ci_{code_for(world.id)}")
    await h.contact(4444, "+77770000000")
    help_msg = h.session.sent(4444)[-1]
    assert help_msg.reply_markup.inline_keyboard[0][0].callback_data == "ci:help"
    await h.callback(4444, 4444, "ci:help")
    assert "помощь на входе" in h.session.last_text(-100)


async def test_web_app_scanner(h, session, world):
    from datetime import datetime as dt

    from aiogram.types import Chat, Message, Update, WebAppData

    link = checkin_codes.deep_link("bc_test_bot", code_for(world.id))
    msg = Message(message_id=1, date=dt.now(UTC), chat=Chat(id=ARU, type="private"), from_user=h.user(ARU),
                  web_app_data=WebAppData(data=link, button_text="✅ Отметиться"))
    await h.feed(Update(update_id=99_001, message=msg))
    assert any("Ты отмечен" in t for t in h.session.texts(ARU))


async def test_volunteer_marks_admin_cancels(h, session, world):
    pid = await session.scalar(select(Participant.id).where(Participant.email == "new@x.kz"))
    await h.callback(VOL, VOL, f"p:card:{pid}")
    card = h.session.sent(VOL)[-1]
    assert card.reply_markup.inline_keyboard[0][0].callback_data == f"ci:mark:{pid}"
    await h.callback(VOL, VOL, f"ci:mark:{pid}")
    method = await session.scalar(select(Checkin.method).where(Checkin.participant_id == pid))
    assert method == CheckinMethod.VOLUNTEER

    await h.callback(VOL, VOL, f"ci:undo:{pid}")  # волонтёру отмена недоступна
    assert await session.scalar(select(func.count()).select_from(Checkin)) == 1
    await h.callback(ADMIN_ID, ADMIN_ID, f"ci:undo:{pid}")
    assert await session.scalar(select(func.count()).select_from(Checkin)) == 0
    assert await session.scalar(select(func.count()).select_from(OutboundEvent).where(OutboundEvent.type == "checkin.cancelled")) == 1


async def test_open_close_and_stats(h, session, world):
    wid = world.id
    await h.text(ADMIN_ID, "/checkin_close")
    session.expire_all()
    assert await session.scalar(select(EventDay.is_checkin_enabled).where(EventDay.id == wid)) is False
    await h.text(ADMIN_ID, "/checkin_open")
    assert await session.scalar(select(EventDay.is_checkin_enabled).where(EventDay.id == wid)) is True

    await h.text(ARU, f"/start ci_{code_for(wid)}")
    await h.text(ADMIN_ID, "/stats")
    assert "отмечено <b>1</b> из 3" in h.session.last_text(ADMIN_ID)
    stats = await day_stats(session, await session.get(EventDay, wid))
    assert stats.teams_partial == ["Rockets (1/3)"]


async def _client(engine, settings) -> TestClient:
    app = web.Application()
    app[SETTINGS], app[SESSIONMAKER] = settings, create_sessionmaker(engine)
    pages.register(app)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


async def test_screen_and_dashboard_endpoints(engine, world, settings, session):
    client = await _client(engine, settings)
    try:
        assert (await client.get("/checkin-screen?key=wrong")).status == 403
        assert (await client.get("/checkin-screen?key=screen-key")).status == 200
        state = await (await client.get("/checkin-screen/state?key=screen-key")).json()
        assert state["state"] == "open" and state["svg"].startswith("<svg") and state["total"] == 3
        assert 1 <= state["seconds_left"] <= 30

        world.is_screen_enabled = False
        await session.commit()
        state = await (await client.get("/checkin-screen/state?key=screen-key")).json()
        assert state["state"] == "screen_off" and "svg" not in state

        assert (await client.get("/dashboard/data?key=screen-key")).status == 403
        data = await (await client.get("/dashboard/data?key=dash-key")).json()
        assert data["day"]["label"] == "День 1" and data["checked"] == 0 and data["teams"][0]["total"] == 3
        assert (await client.get("/webapp/scan")).status == 200
    finally:
        await client.close()


async def test_qr_scan_unlinked_with_matching_username_checks_in_at_once(h, session, world):
    p = await session.scalar(select(Participant).where(Participant.email == "new@x.kz"))
    p.tg_username_form = "new_person"
    await session.commit()
    await h.text(NEW, f"/start ci_{code_for(world.id)}", username="New_Person")
    texts = h.session.texts(NEW)
    assert any("Ты отмечен" in t for t in texts)
    assert not any("Сначала привяжем" in t for t in texts)  # без контакта
