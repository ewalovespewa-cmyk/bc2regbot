"""Этапы 5–6: сегменты, движок рассылок (429/403/повторы/рестарт), опрос явки и отчёты."""
from datetime import UTC, date, datetime, timedelta

import pytest
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.methods import SendMessage
from sqlalchemy import func, select, update

from app.config import Settings
from app.db.models import (
    Admin,
    Broadcast,
    BroadcastDelivery,
    BroadcastStatus,
    Checkin,
    CheckinMethod,
    DeliveryStatus,
    EventDay,
    Participant,
    PollAnswer,
    Role,
    Team,
)
from app.db.session import create_sessionmaker
from app.services import broadcast as engine_mod
from app.services import polls, segments
from tests.harness import Harness

SUPER = 900


@pytest.fixture(autouse=True)
def fast_limiter(monkeypatch):
    monkeypatch.setattr(engine_mod, "limiter", engine_mod.RateLimiter(10_000))


@pytest.fixture
async def world(session):
    """3 команды по 2 человека + 1 соло; 6 привязаны, 1 не открыл бота."""
    teams = [Team(name=n) for n in ("A", "B", "C")]
    solo = Team(name="solo:z@x.kz", is_solo=True)
    session.add_all([*teams, solo])
    await session.flush()
    people = []
    for i, team in enumerate([teams[0], teams[0], teams[1], teams[1], teams[2], teams[2], solo]):
        people.append(
            Participant(
                email=f"p{i}@x.kz", full_name=f"Person {i}", invite_token=f"TOKEN{i:07d}", team_id=team.id,
                telegram_user_id=2000 + i if i != 5 else None,
            )
        )
    session.add_all(people)
    day = EventDay(date=date(2026, 11, 14), label="День 1",
                   checkin_opens_at=datetime(2026, 11, 14, 3, tzinfo=UTC), checkin_closes_at=datetime(2026, 11, 14, 9, tzinfo=UTC))
    session.add(day)
    await session.commit()
    return {"teams": teams, "people": people, "day": day}


@pytest.fixture
def settings():
    return Settings(_env_file=None, superadmin_ids=frozenset({SUPER}), admin_chat_id=-100, test_mode=False)


@pytest.fixture
def h(engine, redis, world, settings) -> Harness:
    return Harness(redis=redis, sessionmaker=create_sessionmaker(engine), settings=settings)


async def _count(session, spec):
    return await segments.count_recipients(session, spec)


async def test_segments(session, world):
    day = world["day"]
    p = world["people"]
    assert await _count(session, {"type": "all"}) == 6
    assert await _count(session, {"type": "team", "team_id": world["teams"][0].id}) == 2
    assert await _count(session, {"type": "solo"}) == 1
    assert await _count(session, {"type": "attendance", "answer": "none"}) == 6  # опроса ещё нет
    assert await _count(session, {"type": "attendance", "answer": "yes"}) == 0

    poll = await polls.create_attendance_poll(session, created_by=SUPER)
    yes, no, maybe = poll.options
    await polls.record_answer(session, poll, p[0], yes)
    await polls.record_answer(session, poll, p[1], maybe)
    await polls.record_answer(session, poll, p[2], no)
    session.add(Checkin(participant_id=p[0].id, event_day_id=day.id, method=CheckinMethod.SELF_QR))
    await session.flush()

    assert await _count(session, {"type": "attendance", "answer": "yes"}) == 1
    assert await _count(session, {"type": "attendance", "answer": "none"}) == 3
    assert await _count(session, {"type": "poll_pending", "poll_id": poll.id}) == 4  # 3 не ответили + «не знаю»
    assert await _count(session, {"type": "checked_in", "day_id": day.id}) == 1
    assert await _count(session, {"type": "not_checked_in", "day_id": day.id}) == 5
    await polls.record_answer(session, poll, p[3], yes)
    assert await _count(session, {"type": "confirmed_not_checked_in", "day_id": day.id}) == 1

    # Заблокировавшие бота не получают рассылки.
    await session.execute(update(Participant).where(Participant.id == p[3].id).values(is_blocked_bot=True))
    assert await _count(session, {"type": "all"}) == 5


async def _make_broadcast(session, settings, segment=None, **kw) -> Broadcast:
    b = Broadcast(title="Тест", text_html="Привет, {name}!", segment=segment or {"type": "all"}, created_by=SUPER, **kw)
    session.add(b)
    await session.flush()
    await engine_mod.start_broadcast(session, b, settings)
    await session.commit()
    return b


async def test_broadcast_delivers_once_handles_403_and_429(h, session, settings, engine):
    p = world_people = (await session.scalars(select(Participant).order_by(Participant.id))).all()
    blocked_chat, slow_chat = p[1].telegram_user_id, p[2].telegram_user_id
    h.session.fail_for[blocked_chat] = TelegramForbiddenError(method=SendMessage(chat_id=1, text="x"), message="blocked")
    calls_429 = {"n": 0}
    original = h.session.make_request

    async def flaky(bot, method, timeout=None):
        if getattr(method, "chat_id", None) == slow_chat and calls_429["n"] == 0:
            calls_429["n"] += 1
            raise TelegramRetryAfter(method=method, message="flood", retry_after=0)
        return await original(bot, method, timeout)

    h.session.make_request = flaky
    b = await _make_broadcast(session, settings)
    sm = create_sessionmaker(engine)
    await engine_mod.run_worker_cycle(h.bot, sm, settings, time_budget=10)

    stats = await engine_mod.delivery_stats(session, b.id)
    assert stats == {"pending": 0, "sent": 5, "failed": 0, "blocked": 1}
    sent_to = [c.chat_id for c in h.session.calls if isinstance(c, SendMessage) and c.chat_id > 1000 and "Привет" in c.text]
    # 6 адресатов (5 доставлено + попытка заблокировавшему), каждому ровно одна успешная отправка;
    # сообщение, получившее 429, повторено после паузы.
    assert calls_429["n"] == 1 and len(sent_to) == len(set(sent_to)) == 6
    assert "Привет, Person!" in [c.text for c in h.session.calls if isinstance(c, SendMessage)]
    bid, blocked_pid = b.id, world_people[1].id
    session.expire_all()
    assert await session.scalar(select(Broadcast.status).where(Broadcast.id == bid)) == BroadcastStatus.DONE
    assert await session.scalar(select(Participant.is_blocked_bot).where(Participant.id == blocked_pid)) is True
    report = [c.text for c in h.session.calls if isinstance(c, SendMessage) and c.chat_id == -100]
    assert report and "Отправлено: 5" in report[-1] and "Заблокировали бота: 1" in report[-1]

    # Повторный проход воркера ничего не шлёт.
    before = len(h.session.calls)
    await engine_mod.run_worker_cycle(h.bot, sm, settings, time_budget=5)
    assert len(h.session.calls) == before


async def test_resume_after_crash_does_not_resend_finished(h, session, settings, engine):
    b = await _make_broadcast(session, settings)
    rows = (await session.scalars(select(BroadcastDelivery).order_by(BroadcastDelivery.id))).all()
    # «Упали» после трёх отправок: три строки sent, одна захвачена давно (claim истёк), две ещё pending.
    for r in rows[:3]:
        r.status, r.attempts = DeliveryStatus.SENT, 1
    rows[3].attempts, rows[3].next_attempt_at = 1, datetime.now(UTC) - timedelta(seconds=1)
    await session.commit()

    await engine_mod.run_worker_cycle(h.bot, create_sessionmaker(engine), settings, time_budget=10)
    resent = {c.chat_id for c in h.session.calls if isinstance(c, SendMessage) and c.chat_id > 1000}
    assert resent == {rows[i].chat_id for i in (3, 4, 5)}


async def test_test_mode_sends_only_to_admins(h, session, engine):
    session.add(Admin(telegram_user_id=901, role=Role.ADMIN))
    await session.commit()
    settings = Settings(_env_file=None, superadmin_ids=frozenset({SUPER}), test_mode=True)
    b = await _make_broadcast(session, settings)
    assert {r.chat_id for r in (await session.scalars(select(BroadcastDelivery))).all()} == {SUPER, 901}
    assert b.stats["test_mode"] is True


async def test_cancel_stops_and_scheduled_starts_on_time(h, session, settings, engine):
    sm = create_sessionmaker(engine)
    b = await _make_broadcast(session, settings)
    assert await engine_mod.cancel_broadcast(session, b)
    await session.commit()
    await engine_mod.run_worker_cycle(h.bot, sm, settings, time_budget=5)
    assert not [c for c in h.session.calls if isinstance(c, SendMessage) and c.chat_id > 1000]

    later = Broadcast(title="Позже", text_html="x", segment={"type": "self"}, created_by=SUPER,
                      status=BroadcastStatus.SCHEDULED, scheduled_at=datetime.now(UTC) + timedelta(hours=1))
    due = Broadcast(title="Пора", text_html="пора!", segment={"type": "self"}, created_by=SUPER,
                    status=BroadcastStatus.SCHEDULED, scheduled_at=datetime.now(UTC) - timedelta(minutes=1))
    session.add_all([later, due])
    await session.commit()
    await engine_mod.run_worker_cycle(h.bot, sm, settings, time_budget=5)
    texts = [c.text for c in h.session.calls if isinstance(c, SendMessage) and c.chat_id == SUPER]
    assert texts[0] == "пора!" and "завершена" in texts[1] and len(texts) == 2  # сообщение + отчёт


async def test_constructor_flow(h, session, settings):
    await h.text(SUPER, "/broadcast")
    await h.callback(SUPER, SUPER, "bc:new")
    await h.text(SUPER, "Анонс: старт в 10:00")
    await h.text(SUPER, "Программа | https://businesscup.zimran.io")
    await h.callback(SUPER, SUPER, "bc:seg:att:none")
    assert "Получателей: <b>6</b>" in h.session.last_text(SUPER)
    await h.callback(SUPER, SUPER, "bc:send")
    b = await session.scalar(select(Broadcast).where(Broadcast.status == BroadcastStatus.RUNNING))
    assert b.buttons == [{"text": "Программа", "url": "https://businesscup.zimran.io"}]
    assert await session.scalar(select(func.count()).select_from(BroadcastDelivery)) == 6


async def test_constructor_schedule_and_template(h, session, settings):
    await h.callback(SUPER, SUPER, "bc:new")
    await h.text(SUPER, "Напоминание")
    await h.callback(SUPER, SUPER, "bc:nobuttons")
    await h.callback(SUPER, SUPER, "bc:seg:self")
    await h.callback(SUPER, SUPER, "bc:tplsave")
    await h.callback(SUPER, SUPER, "bc:schedule")
    when = (datetime.now(UTC) + timedelta(days=2)).astimezone(__import__("zoneinfo").ZoneInfo("Asia/Almaty"))
    await h.text(SUPER, when.strftime("%d.%m %H:%M"))
    assert "запланирована" in h.session.last_text(SUPER)
    assert await session.scalar(select(func.count()).select_from(Broadcast).where(Broadcast.is_template)) == 1
    assert await session.scalar(select(func.count()).select_from(Broadcast).where(Broadcast.status == BroadcastStatus.SCHEDULED)) == 1


async def test_attendance_poll_answers_reason_and_change(h, session, settings, engine):
    p0 = 2000
    await h.text(SUPER, "/poll start")
    poll = await polls.latest_attendance_poll(session)
    yes, no, maybe = poll.options
    poll_id, yes_id, no_id = poll.id, yes.id, no.id
    await engine_mod.run_worker_cycle(h.bot, create_sessionmaker(engine), settings, time_budget=10)
    asked = [c for c in h.session.calls if isinstance(c, SendMessage) and c.chat_id == p0]
    assert asked and asked[-1].reply_markup.inline_keyboard[0][0].callback_data == f"pl:{poll.id}:{yes.id}"

    await h.callback(p0, p0, f"pl:{poll_id}:{no_id}")
    await h.text(p0, "Экзамен")
    pid = await session.scalar(select(Participant.id).where(Participant.telegram_user_id == p0))
    answer = await session.get(PollAnswer, (poll_id, pid))
    assert answer.reason == "Экзамен"

    await h.callback(p0, p0, f"pl:{poll_id}:{yes_id}")  # передумал — можно до дедлайна
    session.expire_all()
    assert (await session.scalar(select(PollAnswer.option_id).where(PollAnswer.participant_id == pid))) == yes_id

    await h.text(SUPER, "/poll close")
    await h.callback(p0, p0, f"pl:{poll_id}:{no_id}")
    assert (await session.scalar(select(PollAnswer.option_id).where(PollAnswer.participant_id == pid))) == yes_id


async def test_reminders_call_list_and_team_report(h, session, settings, world):
    p = world["people"]
    poll = await polls.create_attendance_poll(session, created_by=SUPER, reminder_after_hours=6, max_reminders=1)
    poll.is_active, poll.started_at = True, datetime.now(UTC) - timedelta(hours=7)
    yes, no, maybe = poll.options
    for person in (p[0], p[1]):
        await polls.record_answer(session, poll, person, yes)
    await polls.record_answer(session, poll, p[2], no)
    await polls.record_answer(session, poll, p[3], maybe)
    await session.commit()

    due = await polls.due_reminders(session)
    assert [d.id for d in due] == [poll.id]
    b = await polls.send_reminder(session, poll, settings)
    assert b.stats["total"] == 3  # p3 «не знаю», p4 и соло не ответили (p5 не в боте)
    assert await polls.due_reminders(session) == []  # лимит напоминаний

    header, rows = await polls.call_list(session, poll)
    reasons = {r[2]: r[4] for r in rows}
    assert reasons == {"p3@x.kz": "ответил «не знаю»", "p4@x.kz": "не ответил на опрос",
                       "p5@x.kz": "не открыл бота", "p6@x.kz": "не ответил на опрос"}

    report = await polls.team_report(session, poll)
    assert report.all_confirmed == ["A (2/2)"] and report.someone_declined == ["B (0/2)"]
    assert report.nobody_answered == ["C (0/2)"]


async def test_call_result_from_card(h, session, world):
    poll = await polls.create_attendance_poll(session, created_by=SUPER)
    await session.commit()
    person = world["people"][5]
    await h.callback(SUPER, SUPER, f"pc:ask:{person.id}")
    await h.callback(SUPER, SUPER, f"pc:set:{person.id}:{poll.options[0].id}")
    answer = await session.get(PollAnswer, (poll.id, person.id))
    assert answer.source.value == "call" and answer.answered_by == SUPER
