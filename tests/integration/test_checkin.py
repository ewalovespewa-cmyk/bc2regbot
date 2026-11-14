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
    assert "Есть отметка" in texts[-1] and "Удачи на хакатоне" in texts[-1]
    assert not any("на месте" in x or "Команда" in x for x in texts)  # команду при отметке не показываем
    assert not any("Бинго" in x for x in texts)  # про бинго при отметке молчим

    await h.text(DANA, f"/start ci_{code_for(world.id)}")
    assert "Есть отметка" in h.session.texts(DANA)[-1] and "на месте" not in h.session.texts(DANA)[-1]

    await h.text(ARU, f"/start ci_{code_for(world.id)}")
    assert "Отметка уже есть" in h.session.last_text(ARU)
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
    assert any("Есть отметка" in t for t in h.session.texts(NEW))
    pid = await session.scalar(select(Participant.id).where(Participant.email == "new@x.kz"))
    assert await session.scalar(select(func.count()).select_from(Checkin).where(Checkin.participant_id == pid)) == 1


async def test_phone_mismatch_at_entrance_sends_to_volunteer(h, session, world):
    await h.text(4444, f"/start ci_{code_for(world.id)}")
    await h.contact(4444, "+77770000000")
    hint = h.session.sent(4444)[-1]
    assert "регистрационной стойке" in hint.text and hint.reply_markup is None  # без кнопки
    h.session.clear()
    await h.callback(4444, 4444, "ci:help")  # кнопка из старого сообщения: только подсказка, без алерта админам
    assert h.session.sent(-100) == []


async def test_web_app_scanner(h, session, world):
    from datetime import datetime as dt

    from aiogram.types import Chat, Message, Update, WebAppData

    link = checkin_codes.deep_link("bc_test_bot", code_for(world.id))
    msg = Message(message_id=1, date=dt.now(UTC), chat=Chat(id=ARU, type="private"), from_user=h.user(ARU),
                  web_app_data=WebAppData(data=link, button_text="✅ Отметиться"))
    await h.feed(Update(update_id=99_001, message=msg))
    assert any("Есть отметка" in t for t in h.session.texts(ARU))


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
    assert any("Есть отметка" in t for t in texts)
    assert not any("Сначала привяжем" in t for t in texts)  # без контакта


async def test_screen_command_gives_link_buttons(engine, redis, world):
    settings = Settings(_env_file=None, superadmin_ids=frozenset({SUPER}), webhook_base_url="https://bot.example.com",
                        screen_secret="screen-key", dashboard_secret="dash-key", checkin_secret=SECRET)
    h = Harness(redis=redis, sessionmaker=create_sessionmaker(engine), settings=settings)
    await h.text(ADMIN_ID, "/screen")
    buttons = h.session.sent(ADMIN_ID)[-1].reply_markup.inline_keyboard[0]
    assert buttons[0].url == "https://bot.example.com/checkin-screen?key=screen-key"
    assert buttons[1].url == "https://bot.example.com/dashboard?key=dash-key"

    await h.text(ADMIN_ID, "/admin")
    urls = [b.url for row in h.session.sent(ADMIN_ID)[-1].reply_markup.inline_keyboard for b in row if b.url]
    assert "https://bot.example.com/checkin-screen?key=screen-key" in urls

    await h.text(VOL, "/admin")  # волонтёру ссылки на экран не нужны
    assert not [b.url for row in h.session.sent(VOL)[-1].reply_markup.inline_keyboard for b in row if b.url]


async def test_checkin_survives_failed_reply(h, session, world):
    """Telegram не принял ответ: отметка всё равно сохранена, повторный скан это покажет."""
    day_id = world.id
    from aiogram.exceptions import TelegramRetryAfter

    h.session.fail_for[ARU] = TelegramRetryAfter(method=SendMessage(chat_id=ARU, text="x"), message="flood", retry_after=30)
    await h.text(ARU, f"/start ci_{code_for(day_id)}")
    h.session.fail_for.clear()
    session.expire_all()
    assert await session.scalar(select(func.count()).select_from(Checkin)) == 1

    await h.text(ARU, f"/start ci_{code_for(day_id)}")
    assert "Отметка уже есть" in h.session.last_text(ARU)


# ---------------------------------------------------------------- день финала


def _finals_harness(engine, redis, world) -> Harness:
    s = Settings(_env_file=None, superadmin_ids=frozenset({SUPER}), admin_chat_id=-100, checkin_secret=SECRET,
                 bot_username="bc_test_bot", finals_day=world.date.isoformat())
    return Harness(redis=redis, sessionmaker=create_sessionmaker(engine), settings=s)


async def test_finals_day_only_finalists_check_in(engine, redis, session, world):
    h = _finals_harness(engine, redis, world)
    dana = await session.scalar(select(Participant).where(Participant.telegram_user_id == DANA))
    dana.is_finalist = True
    await session.commit()

    await h.text(ARU, f"/start ci_{code_for(world.id)}")  # Aru не финалист
    assert "только для финалистов" in h.session.last_text(ARU) and "регистрационной стойке" in h.session.last_text(ARU)
    assert await session.scalar(select(func.count()).select_from(Checkin)) == 0
    assert any("Не финалист сканирует QR" in t and "Aru Bekova" in t for t in h.session.texts(-100))

    await h.text(DANA, f"/start ci_{code_for(world.id)}")  # Dana финалист
    assert "Есть отметка" in h.session.last_text(DANA)

    await h.callback(VOL, VOL, f"ci:mark:{(await session.scalar(select(Participant.id).where(Participant.telegram_user_id == ARU)))}")
    assert "нет в списке финалистов" in h.session.last_text(VOL)  # волонтёр может отметить, но видит пометку
    assert await session.scalar(select(func.count()).select_from(Checkin)) == 2


async def test_finals_day_without_list_lets_everyone_in(engine, redis, world):
    h = _finals_harness(engine, redis, world)  # финалистов 0: вкладку не заполнили — никого не блокируем
    await h.text(ARU, f"/start ci_{code_for(world.id)}")
    assert "Есть отметка" in h.session.last_text(ARU)


async def test_finalists_tab_and_segments(session, world):
    from app.services import segments
    from app.services.exports import all_tables

    people = (await session.scalars(select(Participant).order_by(Participant.id))).all()
    people[0].is_finalist = people[1].is_finalist = True  # Aru и Dana финалисты, New — нет
    session.add_all([Checkin(participant_id=people[0].id, event_day_id=world.id, method=CheckinMethod.SELF_QR),
                     Checkin(participant_id=people[2].id, event_day_id=world.id, method=CheckinMethod.VOLUNTEER, checked_by=VOL)])
    await session.commit()

    tables = await all_tables(session, "Asia/Almaty", finals_day=world.date.isoformat())
    tab = tables[-1]
    assert tab.name == "Финалисты · День 1 (BOT)"
    by_name = {r[1]: r for r in tab.rows}
    assert by_name["Aru Bekova"][6] == "да" and by_name["Aru Bekova"][9] == "1/2"
    assert by_name["Dana Ospan"][6] == ""
    assert by_name["New Person"][6] == "да, НЕ финалист"
    assert tab.row_colors == ["C6EFCE", None, "FFEB9C"]

    assert await segments.count_recipients(session, {"type": segments.FINALISTS}) == 2
    # не отмечены сегодня: только Dana (у New нет Telegram, Aru пришла)
    assert await segments.count_recipients(session, {"type": segments.FINALISTS_NOT_CHECKED_IN, "day_id": world.id}) == 1


async def test_sheet_skips_test_day_and_finals_checkin_tab(session, world):
    from app.services.exports import all_tables

    session.add(EventDay(date=world.date - timedelta(days=3), label="Тест", checkin_opens_at=datetime.now(UTC),
                         checkin_closes_at=datetime.now(UTC) + timedelta(hours=1)))
    await session.commit()
    names = [t.name for t in await all_tables(session, "Asia/Almaty")]
    assert names == ["Статус бота (BOT)", "Чекин День 1 (BOT)"]  # тестового дня нет
    assert not any("Тест" in h for h in (await all_tables(session, "Asia/Almaty"))[0].header)
    names = [t.name for t in await all_tables(session, "Asia/Almaty", finals_day=world.date.isoformat())]
    assert names == ["Статус бота (BOT)", "Финалисты · День 1 (BOT)"]  # день финала — только вкладка финалистов
