"""Общий механизм опросов (опрос явки — первый из них; позже можно добавить фидбек и др.).

Опрос рассылается обычной рассылкой с кнопками вариантов. Ответ можно менять до дедлайна.
Вариант с asks_reason просит причину, вариант с keeps_reminding («Пока не знаю») не
останавливает напоминания. Напоминания — тоже рассылки с сегментом poll_pending.
"""
import html
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from aiogram.types import InlineKeyboardButton
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import (
    AnswerSource,
    Broadcast,
    BroadcastStatus,
    Participant,
    Poll,
    PollAnswer,
    PollKind,
    PollOption,
    Team,
)
from app.services import segments
from app.services.importer import team_display_name

ATTENDANCE_OPTIONS = [
    # code, ru, en, asks_reason, keeps_reminding
    ("yes", "Приду ✅", "I'm coming ✅", False, False),
    ("no", "Не смогу ❌", "Can't make it ❌", True, False),
    ("maybe", "Пока не знаю 🤔", "Not sure yet 🤔", False, True),
]
DEFAULT_ATTENDANCE_QUESTION = (
    "Business Cup 2.0 уже совсем скоро: 14–15 ноября, Nazarbayev University, Астана.\n\n<b>Ты придёшь?</b>"
)
DEFAULT_ATTENDANCE_QUESTION_EN = (
    "Business Cup 2.0 is almost here: November 14–15, Nazarbayev University, Astana.\n\n<b>Are you coming?</b>"
)


def option_label(option: PollOption, lang: str) -> str:
    return option.label_en if lang == "en" and option.label_en else option.label_ru


def is_open(poll: Poll, now: datetime | None = None) -> bool:
    now = now or datetime.now(UTC)
    return poll.is_active and (poll.deadline_at is None or now < poll.deadline_at)


async def poll_keyboard_rows(session: AsyncSession, poll_id: int, lang: str) -> list[list[InlineKeyboardButton]]:
    poll = await session.get(Poll, poll_id)
    return [
        [InlineKeyboardButton(text=option_label(o, lang), callback_data=f"pl:{poll.id}:{o.id}")] for o in poll.options
    ]


async def create_attendance_poll(
    session: AsyncSession,
    *,
    created_by: int,
    question_ru: str = DEFAULT_ATTENDANCE_QUESTION,
    question_en: str | None = DEFAULT_ATTENDANCE_QUESTION_EN,
    deadline_at: datetime | None = None,
    reminder_after_hours: int | None = 6,
    max_reminders: int = 1,
) -> Poll:
    poll = Poll(
        kind=PollKind.ATTENDANCE,
        question_ru=question_ru,
        question_en=question_en,
        deadline_at=deadline_at,
        reminder_after_hours=reminder_after_hours,
        max_reminders=max_reminders,
        is_active=False,
        created_by=created_by,
    )
    session.add(poll)
    await session.flush()
    for position, (code, ru, en, reason, keep) in enumerate(ATTENDANCE_OPTIONS):
        session.add(
            PollOption(
                poll_id=poll.id, code=code, label_ru=ru, label_en=en, position=position, asks_reason=reason,
                keeps_reminding=keep,
            )
        )
    await session.flush()
    await session.refresh(poll, ["options"])
    return poll


async def launch_poll(session: AsyncSession, poll: Poll, settings: Settings, *, segment: dict | None = None) -> Broadcast:
    """Активирует опрос и создаёт рассылку с кнопками (запуск рассылки — start_broadcast)."""
    from app.services.broadcast import start_broadcast

    poll.is_active = True
    poll.started_at = datetime.now(UTC)
    b = Broadcast(
        title=f"Опрос #{poll.id}",
        text_html=poll.question_ru,
        segment=segment or {"type": segments.ALL},
        poll_id=poll.id,
        created_by=poll.created_by,
        status=BroadcastStatus.DRAFT,
    )
    session.add(b)
    await session.flush()
    await start_broadcast(session, b, settings)
    return b


async def record_answer(
    session: AsyncSession,
    poll: Poll,
    participant: Participant,
    option: PollOption,
    *,
    source: AnswerSource = AnswerSource.BOT,
    answered_by: int | None = None,
    reason: str | None = None,
) -> None:
    values = dict(
        poll_id=poll.id,
        participant_id=participant.id,
        option_id=option.id,
        source=source,
        answered_by=answered_by,
        reason=reason,
    )
    stmt = insert(PollAnswer).values(**values)
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=[PollAnswer.poll_id, PollAnswer.participant_id],
            set_={
                "option_id": stmt.excluded.option_id,
                "source": stmt.excluded.source,
                "answered_by": stmt.excluded.answered_by,
                "reason": stmt.excluded.reason,
                "updated_at": datetime.now(UTC),
            },
        )
    )


async def set_reason(session: AsyncSession, poll_id: int, participant_id: int, reason: str) -> None:
    answer = await session.get(PollAnswer, (poll_id, participant_id))
    if answer:
        answer.reason = reason[:1000]


async def due_reminders(session: AsyncSession, now: datetime | None = None) -> list[Poll]:
    now = now or datetime.now(UTC)
    polls = (await session.scalars(select(Poll).where(Poll.is_active, Poll.reminder_after_hours.is_not(None)))).all()
    due = []
    for poll in polls:
        if not is_open(poll, now) or poll.reminders_sent >= poll.max_reminders or not poll.started_at:
            continue
        last = poll.last_reminder_at or poll.started_at
        if now - last >= timedelta(hours=poll.reminder_after_hours):
            due.append(poll)
    return due


async def send_reminder(session: AsyncSession, poll: Poll, settings: Settings) -> Broadcast | None:
    from app.services.broadcast import start_broadcast

    segment = {"type": segments.POLL_PENDING, "poll_id": poll.id}
    poll.reminders_sent += 1
    poll.last_reminder_at = datetime.now(UTC)
    if await segments.count_recipients(session, segment) == 0:
        return None
    b = Broadcast(
        title=f"Напоминание по опросу #{poll.id}",
        text_html="⏰ <b>Напоминание</b>\n\n" + poll.question_ru,
        segment=segment,
        poll_id=poll.id,
        created_by=poll.created_by,
    )
    session.add(b)
    await session.flush()
    await start_broadcast(session, b, settings)
    return b


# ---------------------------------------------------------------- отчёты


async def latest_attendance_poll(session: AsyncSession) -> Poll | None:
    return await session.scalar(
        select(Poll).where(Poll.kind == PollKind.ATTENDANCE).order_by(Poll.created_at.desc()).limit(1)
    )


async def _answers_by_participant(session: AsyncSession, poll_id: int) -> dict[int, tuple[PollOption, PollAnswer]]:
    rows = await session.execute(
        select(PollAnswer, PollOption).join(PollOption, PollOption.id == PollAnswer.option_id).where(PollAnswer.poll_id == poll_id)
    )
    return {a.participant_id: (o, a) for a, o in rows}


async def call_list(session: AsyncSession, poll: Poll | None) -> tuple[list[str], list[list[str]]]:
    """Кого обзвонить: не открыли бота, не ответили, ответили «не знаю»."""
    answers = await _answers_by_participant(session, poll.id) if poll else {}
    participants = (
        await session.scalars(select(Participant).where(Participant.is_selected).order_by(Participant.full_name))
    ).all()
    header = ["full_name", "phone", "email", "team", "reason", "telegram"]
    rows = []
    for p in participants:
        answer = answers.get(p.id)
        if p.telegram_user_id is None:
            why = "не открыл бота"
        elif answer is None:
            why = "не ответил на опрос"
        elif answer[0].keeps_reminding:
            why = "ответил «не знаю»"
        else:
            continue
        rows.append([p.full_name, p.phone or p.phone_raw or "", p.email, team_display_name(p.team), why,
                     f"@{p.telegram_username}" if p.telegram_username else ""])
    return header, rows


@dataclass
class TeamReport:
    all_confirmed: list[str] = field(default_factory=list)
    someone_declined: list[str] = field(default_factory=list)
    nobody_answered: list[str] = field(default_factory=list)
    partial: list[str] = field(default_factory=list)


async def team_report(session: AsyncSession, poll: Poll) -> TeamReport:
    answers = await _answers_by_participant(session, poll.id)
    teams = (await session.scalars(select(Team).where(Team.is_solo.is_(False)).order_by(Team.name))).all()
    report = TeamReport()
    for team in teams:
        members = (
            await session.scalars(select(Participant).where(Participant.team_id == team.id, Participant.is_selected))
        ).all()
        if not members:
            continue
        codes = [answers[m.id][0].code if m.id in answers else None for m in members]
        line = f"{team.name} ({sum(c == 'yes' for c in codes)}/{len(codes)})"
        if all(c == "yes" for c in codes):
            report.all_confirmed.append(line)
        elif any(c == "no" for c in codes):
            report.someone_declined.append(line)
        elif all(c is None for c in codes):
            report.nobody_answered.append(line)
        else:
            report.partial.append(line)
    return report


async def poll_summary(session: AsyncSession, poll: Poll) -> str:
    answers = await _answers_by_participant(session, poll.id)
    counts = {o.code: 0 for o in poll.options}
    calls = 0
    for option, answer in answers.values():
        counts[option.code] = counts.get(option.code, 0) + 1
        calls += answer.source == AnswerSource.CALL
    linked = await session.scalar(
        select(func.count()).select_from(Participant).where(
            Participant.is_selected, Participant.telegram_user_id.is_not(None)
        )
    )
    total = await session.scalar(select(func.count()).select_from(Participant).where(Participant.is_selected))
    lines = [f"📋 <b>Опрос #{poll.id}</b> · {'идёт' if is_open(poll) else 'закрыт'}"]
    for o in poll.options:
        lines.append(f"{html.escape(o.label_ru)}: {counts.get(o.code, 0)}")
    lines.append(f"Не ответили: {max((linked or 0) - len(answers), 0)} из {linked} в боте")
    lines.append(f"Не открыли бота: {(total or 0) - (linked or 0)}")
    if calls:
        lines.append(f"Из них внесено по обзвону: {calls}")
    lines.append(f"Напоминаний отправлено: {poll.reminders_sent}/{poll.max_reminders}")
    return "\n".join(lines)
