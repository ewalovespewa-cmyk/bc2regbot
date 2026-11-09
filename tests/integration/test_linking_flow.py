"""Сквозные сценарии этапа 3: апдейты Telegram → диспетчер → Postgres → ответы бота."""
import pytest
from sqlalchemy import func, select

from app.config import Settings
from app.db.models import OutboundEvent, Participant, SecurityEvent, Team
from app.db.session import create_sessionmaker
from tests.harness import Harness

ADMIN_CHAT = -1009999
SUPERADMIN = 900
ARU, DANA, STRANGER = 1001, 1002, 1003


@pytest.fixture
async def seeded(session):
    team = Team(name="Rockets")
    session.add(team)
    await session.flush()
    session.add_all(
        [
            Participant(email="aru@mail.com", full_name="Aru Bekova", phone="+77011112233", invite_token="ARUtoken0001",
                        team_id=team.id, tg_username_form="aru_b"),
            Participant(email="dana@mail.com", full_name="Dana Ospan", phone="+77022223344", invite_token="DANAtoken001",
                        team_id=team.id),
            Participant(email="gone@mail.com", full_name="Was Deselected", phone="+77033334455", invite_token="GONEtoken001",
                        is_selected=False),
        ]
    )
    await session.commit()


@pytest.fixture
def h(engine, redis, seeded) -> Harness:
    settings = Settings(
        _env_file=None,
        admin_chat_id=ADMIN_CHAT,
        superadmin_ids=frozenset({SUPERADMIN}),
        organizer_contact="@bc_org",
        games_bot_username="bc_games_bot",
        bot_username="bc_test_bot",
    )
    return Harness(redis=redis, sessionmaker=create_sessionmaker(engine), settings=settings)


async def participant(session, email) -> Participant:
    session.expire_all()
    return await session.scalar(select(Participant).where(Participant.email == email))


async def test_link_by_token(h, session):
    await h.text(ARU, "/start ARUtoken0001", username="Aru_B", lang="en")
    p = await participant(session, "aru@mail.com")
    assert p.telegram_user_id == ARU and p.telegram_username == "aru_b" and p.lang == "en"
    welcome = h.session.texts(ARU)[0]
    assert "Aru" in welcome and "congrats" in welcome and "t.me/business_cup_2026" in welcome
    assert "Rockets" not in welcome and "Team:" not in welcome  # команду в приветствии не показываем
    assert any("photo hunt" in t for t in h.session.texts(ARU))  # кнопка в бот игр
    assert await session.scalar(select(func.count()).select_from(OutboundEvent)) == 1  # событие для бота игр


async def test_repeated_start_is_welcome_back(h, session):
    await h.text(ARU, "/start ARUtoken0001")
    h.session.clear()
    await h.text(ARU, "/start ARUtoken0001")
    await h.text(ARU, "/start")
    assert all("С возвращением" in t for t in h.session.texts(ARU))
    assert await session.scalar(select(func.count()).select_from(OutboundEvent)) == 1


async def test_token_of_someone_else_refused_and_alerted(h, session):
    await h.text(ARU, "/start ARUtoken0001")
    h.session.clear()
    await h.text(STRANGER, "/start ARUtoken0001", username="thief")
    assert "уже привязана к другому" in h.session.last_text(STRANGER)
    assert "чужую заявку" in h.session.last_text(ADMIN_CHAT)
    assert (await participant(session, "aru@mail.com")).telegram_user_id == ARU
    assert await session.scalar(select(func.count()).select_from(SecurityEvent)) == 1


async def test_second_participant_on_same_account_refused(h, session):
    await h.text(ARU, "/start ARUtoken0001")
    await h.text(ARU, "/start DANAtoken001")
    assert "уже привязан к другому участнику" in h.session.last_text(ARU)
    assert (await participant(session, "dana@mail.com")).telegram_user_id is None
    assert "Аккаунт уже привязан" in h.session.last_text(ADMIN_CHAT)


async def test_unknown_start_asks_for_contact_then_links_by_phone(h, session):
    await h.text(DANA, "/start")
    ask = h.session.sent(DANA)[-1]
    assert ask.reply_markup.keyboard[0][0].request_contact is True

    await h.contact(DANA, "87022223344")  # без плюса, с 8 — как бывает в контакте
    assert (await participant(session, "dana@mail.com")).telegram_user_id == DANA
    assert any("Dana" in t for t in h.session.texts(DANA))


async def test_foreign_contact_refused(h, session):
    await h.contact(DANA, "+77022223344", contact_user_id=555)
    assert "твой" in h.session.last_text(DANA)
    assert (await participant(session, "dana@mail.com")).telegram_user_id is None


async def test_unknown_phone_and_deselected_get_polite_refusal(h, session):
    await h.contact(STRANGER, "+77770000000")
    assert "Не нашли тебя" in h.session.last_text(STRANGER) and "@bc_org" in h.session.last_text(STRANGER)
    h.session.clear()
    await h.text(STRANGER, "/start GONEtoken001")  # снят с отбора — как будто нет
    assert h.session.sent(STRANGER)[-1].reply_markup.keyboard[0][0].request_contact is True


async def test_username_from_form_as_fallback(h, session):
    await h.contact(ARU, "+79990000000", username="ARU_B")  # номер в Telegram другой, username совпал
    assert (await participant(session, "aru@mail.com")).telegram_user_id == ARU


async def test_menu_hidden_for_unregistered(h, session):
    await h.text(STRANGER, "👤 Мой статус")
    assert h.session.sent(STRANGER)[-1].reply_markup.keyboard[0][0].request_contact is True


async def test_status_and_pages(h, session):
    await h.text(ARU, "/start ARUtoken0001")
    await h.text(DANA, "/start DANAtoken001")
    h.session.clear()
    await h.text(ARU, "👤 Мой статус")
    status = h.session.last_text(ARU)
    assert "Aru Bekova" in status and "Первый этап отбора пройден" in status and "t.me/business_cup_2026" in status
    assert "Rockets" not in status and "Dana" not in status and "Команда" not in status
    assert "Опрос" not in status and "в боте" not in status and "✅ Dana" not in status

    await h.text(ARU, "✉️ Задать вопрос")  # кнопка со старой клавиатуры: просто показываем меню заново
    assert "Выбери пункт" in h.session.last_text(ARU)
    await h.text(ARU, "❓ FAQ")
    assert "Выбери пункт" in h.session.last_text(ARU)

    await h.text(ARU, "📍 Локация")
    assert any(type(c).__name__ == "SendLocation" for c in h.session.sent(ARU))
    await h.text(ARU, "☎️ Контакты")
    assert "@bc_org" in h.session.last_text(ARU)


async def test_language_switch(h, session):
    await h.text(ARU, "/start ARUtoken0001")
    await h.text(ARU, "🌐 English")
    assert (await participant(session, "aru@mail.com")).lang == "en"
    await h.text(ARU, "👤 My status")
    assert "My status" in h.session.last_text(ARU)


async def test_duplicate_update_processed_once(h, session):
    await h.text(ARU, "/start ARUtoken0001", update_id=777)
    await h.text(ARU, "/start ARUtoken0001", update_id=777)
    assert len(h.session.sent(ARU)) == 3  # поздравление + меню + подсказка про игры, только один раз


async def test_start_links_silently_by_telegram_username(h, session):
    await h.text(ARU, "/start", username="Aru_B")  # username в Telegram совпал с формой
    assert (await participant(session, "aru@mail.com")).telegram_user_id == ARU
    assert "Aru" in h.session.texts(ARU)[0]
    assert all(
        not (getattr(c.reply_markup, "keyboard", None) and c.reply_markup.keyboard[0][0].request_contact)
        for c in h.session.sent(ARU)
    )  # номер не спрашивали


async def test_username_mismatch_still_asks_contact(h, session):
    await h.text(DANA, "/start", username="someone_else")
    assert h.session.sent(DANA)[-1].reply_markup.keyboard[0][0].request_contact is True
    assert (await participant(session, "dana@mail.com")).telegram_user_id is None


async def test_ambiguous_username_asks_contact(h, session):
    session.add(Participant(email="dup@mail.com", full_name="Dup", invite_token="DUPtoken0001", tg_username_form="aru_b"))
    await session.commit()
    await h.text(ARU, "/start", username="aru_b")
    assert h.session.sent(ARU)[-1].reply_markup.keyboard[0][0].request_contact is True


async def test_account_held_by_deselected_record_moves_to_active_one(h, session):
    """Старая запись (снята с отбора) держит Telegram — новая заявка с тем же username всё равно привязывается."""
    old = await participant(session, "gone@mail.com")  # is_selected=False
    old.telegram_user_id, old.tg_username_form = STRANGER, "akhmat_tg"
    session.add(Participant(email="anton@mail.com", full_name="Антон", phone="+77759599875",
                            invite_token="ANTONtoken01", tg_username_form="akhmat_tg"))
    await session.commit()

    await h.text(STRANGER, "/start", username="Akhmat_TG")
    assert (await participant(session, "anton@mail.com")).telegram_user_id == STRANGER
    assert (await participant(session, "gone@mail.com")).telegram_user_id is None
    assert "Антон" in h.session.texts(STRANGER)[0]  # приветствие, номер не спрашивали


def test_first_message_wording():
    from app.i18n import t

    text = t("start.welcome", "en", name="Amira", channel="https://t.me/business_cup_2026")
    assert text.endswith("All the main news will be there!") and "Team" not in text


def test_participants_are_not_told_about_personal_links():
    from pathlib import Path as P

    for lang in ("ru", "en"):
        text = (P("app/i18n") / f"{lang}.yaml").read_text(encoding="utf-8").lower()
        assert "из письма" not in text and "ссылка уже" not in text and "from your email" not in text


async def test_green_hall_only_for_finalists(h, session):
    from app.services.importer import ImportAborted, mark_finalists

    await h.text(ARU, "/start ARUtoken0001")
    await h.text(DANA, "/start DANAtoken001")

    async def location(uid):
        await h.text(uid, "📍 Локация")
        return h.session.last_text(uid)

    assert "Orange Hall" in await location(ARU) and "Green Hall" not in await location(ARU)

    assert await mark_finalists(session, [{"Почта": "ARU@mail.com"}, {"Почта": "ghost@mail.com"}]) == (1, ["ghost@mail.com"])
    await session.commit()
    text = await location(ARU)
    assert "Orange Hall" in text and "Green Hall" in text
    assert "Green Hall" not in await location(DANA)

    await mark_finalists(session, [])  # вкладку очистили
    await session.commit()
    assert "Green Hall" not in await location(ARU)

    with pytest.raises(ImportAborted, match="почтой"):
        await mark_finalists(session, [{"Имя": "Без почты"}])


async def test_finalists_tab_is_created_when_missing(session, monkeypatch):
    from app.config import Settings
    from app.services import sheet_sync
    from app.services.sources import SheetMissing

    made = []

    class FakeClient:
        def __init__(self, settings):
            pass

        async def read_rows(self, worksheet):
            raise SheetMissing("нет вкладки")

        async def create_source_tab(self, name):
            made.append(name)

    monkeypatch.setattr(sheet_sync, "SheetsClient", FakeClient)
    settings = Settings(_env_file=None, sheet_id="x", google_service_account_json="{}")
    assert await sheet_sync.sync_finalists(session, settings) == (0, [])
    assert made == ["Финал (BOT)"]


async def test_status_shows_first_stage_without_team(h, session):
    solo = Participant(email="solo@mail.com", full_name="Solo Person", phone="+77044445566", invite_token="SOLOtoken001",
                       participation_type="Solo participant")
    session.add(solo)
    await session.commit()

    await h.text(5001, "/start SOLOtoken001")
    assert "Solo Participant" not in h.session.texts(5001)[0]
    await h.text(5001, "👤 Мой статус")
    status = h.session.last_text(5001)
    assert "Первый этап отбора пройден" in status and "Solo Participant" not in status and "Команда" not in status

    await h.text(5001, "🌐 English")
    await h.text(5001, "👤 My status")
    assert "First selection stage passed" in h.session.last_text(5001)
