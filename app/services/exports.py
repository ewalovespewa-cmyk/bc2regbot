"""Выгрузка статусов участников: лист bot_status в Google Sheet или CSV.

Строка = участник. Отсюда команда берёт персональную ссылку для email-рассылки
и видит, кто запустил бота, что ответил на опрос и отметился ли.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import Checkin, EventDay, Participant, Poll, PollAnswer, PollKind, PollOption
from app.services.importer import team_display_name
from app.services.tokens import invite_link

YES, NO = "да", "нет"


def _fmt(dt: datetime | None, tz: ZoneInfo) -> str:
    return dt.astimezone(tz).strftime("%Y-%m-%d %H:%M") if dt else ""


async def bot_status_table(session: AsyncSession, bot_username: str, timezone: str) -> tuple[list[str], list[list[str]]]:
    tz = ZoneInfo(timezone)
    days = (await session.scalars(select(EventDay).order_by(EventDay.date))).all()

    attendance = await session.scalar(
        select(Poll).where(Poll.kind == PollKind.ATTENDANCE).order_by(Poll.created_at.desc()).limit(1)
    )
    answers: dict[int, tuple[str, str]] = {}
    if attendance:
        result = await session.execute(
            select(PollAnswer.participant_id, PollOption.label_ru, PollAnswer.source)
            .join(PollOption, PollOption.id == PollAnswer.option_id)
            .where(PollAnswer.poll_id == attendance.id)
        )
        answers = {pid: (label, source) for pid, label, source in result}

    checkins: dict[tuple[int, int], datetime] = {
        (c.participant_id, c.event_day_id): c.created_at for c in (await session.scalars(select(Checkin))).all()
    }

    participants = (
        await session.scalars(
            select(Participant)
            .where(Participant.is_selected)
            .options(selectinload(Participant.team))
            .order_by(Participant.id)
        )
    ).all()

    header = [
        "email",
        "full_name",
        "phone",
        "team",
        "invite_link",
        "bot_started",
        "linked_at",
        "telegram_username",
        "blocked_bot",
        "attendance",
        *[f"checkin_{d.label}" for d in days],
    ]
    rows = []
    for p in participants:
        answer = answers.get(p.id)
        answer_text = ""
        if answer:
            answer_text = answer[0] + (" (обзвон)" if answer[1] == "call" else "")
        rows.append(
            [
                p.email,
                p.full_name,
                p.phone or p.phone_raw or "",
                team_display_name(p.team),
                invite_link(bot_username, p.invite_token) if bot_username else p.invite_token,
                YES if p.telegram_user_id else NO,
                _fmt(p.linked_at, tz),
                f"@{p.telegram_username}" if p.telegram_username else "",
                YES if p.is_blocked_bot else "",
                answer_text,
                *[_fmt(checkins.get((p.id, d.id)), tz) for d in days],
            ]
        )
    return header, rows
