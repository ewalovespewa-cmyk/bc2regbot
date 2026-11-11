"""Анти-спам: человек, который жмёт кнопку без остановки, не грузит бота."""
import pytest
from aiogram.methods import AnswerCallbackQuery, SendMessage
from sqlalchemy import select

from app.config import Settings
from app.db.models import Participant, Team
from app.db.session import create_sessionmaker
from tests.harness import Harness

ARU, SPAMMER, ADMIN = 1001, 1666, 900


@pytest.fixture
async def seeded(session):
    team = Team(name="Rockets")
    session.add(team)
    await session.flush()
    session.add(Participant(email="aru@mail.com", full_name="Aru Bekova", phone="+77011112233", invite_token="ARUtoken0001",
                            team_id=team.id, telegram_user_id=ARU))
    await session.commit()


@pytest.fixture
def h(engine, redis, seeded) -> Harness:
    settings = Settings(_env_file=None, superadmin_ids=frozenset({ADMIN}), throttle_limit=6, throttle_window_seconds=60)
    return Harness(redis=redis, sessionmaker=create_sessionmaker(engine), settings=settings)


def _texts(h, uid):
    return [c.text for c in h.session.calls if isinstance(c, SendMessage) and c.chat_id == uid]


async def test_spammed_button_is_cut_off(h):
    for _ in range(20):
        await h.text(ARU, "👤 Мой статус")
    texts = _texts(h, ARU)
    assert sum("Мой статус" in t for t in texts) == 6  # обработано ровно по лимиту
    assert sum("Не так быстро" in t for t in texts) == 1  # предупреждение одно


async def test_spammed_callback_answered_once(h):
    for _ in range(15):
        await h.callback(SPAMMER, SPAMMER, "pl:1:1")
    warned = [c for c in h.session.calls if isinstance(c, AnswerCallbackQuery) and "Не так быстро" in (c.text or "")]
    assert len(warned) == 1


async def test_other_people_not_affected(h):
    for _ in range(20):
        await h.callback(SPAMMER, SPAMMER, "pl:1:1")
    await h.text(ARU, "👤 Мой статус")
    assert any("Мой статус" in t for t in _texts(h, ARU))


async def test_album_is_not_spam(h):
    await h.callback(ADMIN, ADMIN, "bc:new")
    for n in range(10):
        await h.photo(ADMIN, "Agenda" if n == 0 else None, media_group_id="G", file_id=f"card-{n}")
    assert not any("Не так быстро" in t for t in _texts(h, ADMIN))


async def test_checkin_scan_never_throttled(h):
    for _ in range(10):
        await h.text(ARU, "👤 Мой статус")
    h.session.clear()
    await h.text(ARU, "/start ci_WRONGCODE1")  # пришёл на вход после спама — скан всё равно обрабатывается
    assert not any("Не так быстро" in t for t in _texts(h, ARU)) and _texts(h, ARU)
