"""Этап 4: роли, /admin, /find и карточка, отвязка, редактирование контента."""
import pytest
from sqlalchemy import func, select

from app.config import Settings
from app.db.models import Admin, AdminAction, ContentPage, Participant, Role, Team
from app.db.session import create_sessionmaker
from app.services.search import search_participants
from tests.harness import Harness

SUPER, ADMIN_ID, VOL_ID, ARU = 900, 901, 902, 1001


@pytest.fixture
async def seeded(session):
    team = Team(name="Rockets")
    session.add(team)
    await session.flush()
    session.add_all(
        [
            Participant(email="aru@mail.com", full_name="Aru Bekova", phone="+77011112233", invite_token="ARUtoken0001",
                        team_id=team.id),
            Participant(email="dana@mail.com", full_name="Dana Ospanova", phone="+77022223344", invite_token="DANAtoken001",
                        team_id=team.id),
        ]
    )
    await session.commit()


@pytest.fixture
def h(engine, redis, seeded) -> Harness:
    settings = Settings(_env_file=None, superadmin_ids=frozenset({SUPER}), admin_chat_id=-100)
    return Harness(redis=redis, sessionmaker=create_sessionmaker(engine), settings=settings)


async def test_search_variants(session, seeded):
    names = lambda ps: [p.full_name for p in ps]  # noqa: E731
    assert names(await search_participants(session, "беков")) == []
    assert names(await search_participants(session, "Bekova")) == ["Aru Bekova"]
    assert names(await search_participants(session, "aru bek")) == ["Aru Bekova"]
    assert names(await search_participants(session, "8 702 222 33 44")) == ["Dana Ospanova"]
    assert names(await search_participants(session, "2223344")) == ["Dana Ospanova"]
    assert names(await search_participants(session, "dana@mail")) == ["Dana Ospanova"]
    assert sorted(names(await search_participants(session, "rockets"))) == ["Aru Bekova", "Dana Ospanova"]
    assert await search_participants(session, "%") == []


async def test_addadmin_by_id_and_username_and_remove(h, session):
    await h.text(ADMIN_ID, "/start", username="Org_Admin")  # бот запоминает username
    await h.text(SUPER, "/addadmin @org_admin admin")
    await h.text(SUPER, f"/addadmin {VOL_ID} volunteer")
    roles = {a.telegram_user_id: a.role for a in (await session.scalars(select(Admin))).all()}
    assert roles == {ADMIN_ID: Role.ADMIN, VOL_ID: Role.VOLUNTEER}

    await h.text(SUPER, "/addadmin @nobody admin")
    assert "Не знаю такого username" in h.session.last_text(SUPER)

    await h.text(SUPER, f"/removeadmin {VOL_ID}")
    session.expire_all()
    assert await session.scalar(select(func.count()).select_from(Admin)) == 1
    assert await session.scalar(select(func.count()).select_from(AdminAction)) == 3


async def test_non_superadmin_cannot_manage_roles(h, session):
    await h.text(SUPER, f"/addadmin {ADMIN_ID} admin")
    await h.text(ADMIN_ID, f"/addadmin {VOL_ID} admin")
    assert await session.scalar(select(func.count()).select_from(Admin)) == 1


async def test_volunteer_finds_but_cannot_export(h, session):
    await h.text(SUPER, f"/addadmin {VOL_ID} volunteer")
    h.session.clear()
    await h.text(VOL_ID, "/admin")
    menu = h.session.sent(VOL_ID)[-1].reply_markup.inline_keyboard
    assert [b.callback_data for row in menu for b in row] == ["adm:find"]

    await h.text(VOL_ID, "/find Bekova")
    result = h.session.sent(VOL_ID)[-1]
    data = result.reply_markup.inline_keyboard[0][0].callback_data
    await h.callback(VOL_ID, VOL_ID, data)
    card = h.session.last_text(VOL_ID)
    assert "Aru Bekova" in card and "Dana Ospanova" in card and "aru@mail.com" not in card  # email только админам

    before = len(h.session.sent(VOL_ID))
    await h.text(VOL_ID, "/export")
    assert all(type(c).__name__ != "SendDocument" for c in h.session.sent(VOL_ID)[before:])


async def test_find_via_menu_state(h, session):
    await h.text(SUPER, "/find")
    await h.text(SUPER, "rockets")
    assert "Нашёл: 2" in h.session.last_text(SUPER)


async def test_unlink(h, session):
    await h.text(ARU, "/start ARUtoken0001")
    pid = await session.scalar(select(Participant.id).where(Participant.email == "aru@mail.com"))
    await h.callback(SUPER, SUPER, f"p:unlink:{pid}")
    await h.callback(SUPER, SUPER, f"p:unlink_ok:{pid}")
    assert await session.scalar(select(Participant.telegram_user_id).where(Participant.id == pid)) is None


async def test_edit_content(h, session):
    await h.text(SUPER, "/content")
    await h.callback(SUPER, SUPER, "c:edit:faq:ru")
    await h.text(SUPER, "Бейджи выдаём на входе")
    assert "Предпросмотр" in "".join(h.session.texts(SUPER))
    await h.callback(SUPER, SUPER, "c:save")
    page = await session.get(ContentPage, ("faq", "ru"))
    assert page.text_html == "Бейджи выдаём на входе"

    await h.text(ARU, "/start ARUtoken0001")
    await h.text(ARU, "❓ FAQ")
    assert h.session.last_text(ARU) == "Бейджи выдаём на входе"

    await h.callback(SUPER, SUPER, "c:reset:faq:ru")
    session.expire_all()
    assert await session.get(ContentPage, ("faq", "ru")) is None
