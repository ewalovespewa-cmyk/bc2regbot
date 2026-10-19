"""Сегменты получателей рассылок.

Сегмент — JSON вида {"type": "...", ...параметры}; хранится в broadcasts.segment.
Базовое условие для всех: участник отобран, привязал Telegram и не заблокировал бота.
"""
from typing import Any

from sqlalchemy import Select, exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Checkin, EventDay, Participant, Poll, PollAnswer, PollKind, PollOption, Team

ALL = "all"
ATTENDANCE = "attendance"  # answer: yes | no | maybe | none
CHECKED_IN = "checked_in"  # day_id
NOT_CHECKED_IN = "not_checked_in"  # day_id
CONFIRMED_NOT_CHECKED_IN = "confirmed_not_checked_in"  # day_id — «Мы тебя ждём»
TEAM = "team"  # team_id
SOLO = "solo"
SELF = "self"  # тестовая отправка только себе
POLL_PENDING = "poll_pending"  # poll_id — для напоминаний: не ответил или ответ «ещё не решил»

ANSWER_TITLES = {"yes": "ответили «Приду»", "no": "ответили «Не смогу»", "maybe": "ответили «Не знаю»", "none": "не ответили на опрос"}


def recipients_base() -> Select:
    return select(Participant.id, Participant.telegram_user_id).where(
        Participant.is_selected, Participant.telegram_user_id.is_not(None), Participant.is_blocked_bot.is_(False)
    )


async def latest_attendance_poll_id(session: AsyncSession) -> int | None:
    return await session.scalar(
        select(Poll.id).where(Poll.kind == PollKind.ATTENDANCE).order_by(Poll.created_at.desc()).limit(1)
    )


def _answered(poll_id: int, codes: list[str] | None = None):
    cond = [PollAnswer.poll_id == poll_id, PollAnswer.participant_id == Participant.id]
    if codes is not None:
        cond += [PollOption.id == PollAnswer.option_id, PollOption.code.in_(codes)]
        return exists().where(*cond).correlate(Participant)
    return exists().where(*cond).correlate(Participant)


def _checked(day_id: int):
    return exists().where(Checkin.participant_id == Participant.id, Checkin.event_day_id == day_id).correlate(Participant)


async def segment_query(session: AsyncSession, spec: dict[str, Any]) -> Select | None:
    """SELECT (participant_id, telegram_user_id) получателей. None — для SELF (решается в рассылке)."""
    kind = spec.get("type", ALL)
    stmt = recipients_base()
    if kind == ALL:
        return stmt
    if kind == SELF:
        return None
    if kind == ATTENDANCE:
        poll_id = spec.get("poll_id") or await latest_attendance_poll_id(session)
        answer = spec["answer"]
        if poll_id is None:
            # Опроса ещё не было: «не ответили» — все, остальные варианты — никто.
            return stmt if answer == "none" else stmt.where(False)
        if answer == "none":
            return stmt.where(~_answered(poll_id))
        return stmt.where(_answered(poll_id, [answer]))
    if kind == POLL_PENDING:
        poll_id = spec["poll_id"]
        undecided = select(PollOption.code).where(PollOption.poll_id == poll_id, PollOption.keeps_reminding)
        codes = list((await session.scalars(undecided)).all())
        return stmt.where(~_answered(poll_id) | _answered(poll_id, codes))
    if kind == CHECKED_IN:
        return stmt.where(_checked(spec["day_id"]))
    if kind == NOT_CHECKED_IN:
        return stmt.where(~_checked(spec["day_id"]))
    if kind == CONFIRMED_NOT_CHECKED_IN:
        poll_id = spec.get("poll_id") or await latest_attendance_poll_id(session)
        if poll_id is None:
            return stmt.where(False)
        return stmt.where(_answered(poll_id, ["yes"]), ~_checked(spec["day_id"]))
    if kind == TEAM:
        return stmt.where(Participant.team_id == spec["team_id"])
    if kind == SOLO:
        solo = exists().where(Team.id == Participant.team_id, Team.is_solo).correlate(Participant)
        return stmt.where(Participant.team_id.is_(None) | solo)
    raise ValueError(f"Неизвестный сегмент: {kind}")


async def count_recipients(session: AsyncSession, spec: dict[str, Any]) -> int:
    stmt = await segment_query(session, spec)
    if stmt is None:
        return 1
    return await session.scalar(select(func.count()).select_from(stmt.subquery())) or 0


async def describe(session: AsyncSession, spec: dict[str, Any]) -> str:
    kind = spec.get("type", ALL)
    day = None
    if "day_id" in spec:
        day = await session.get(EventDay, spec["day_id"])
    day_label = day.label if day else "?"
    if kind == ALL:
        return "все привязанные участники"
    if kind == ATTENDANCE:
        return ANSWER_TITLES.get(spec["answer"], spec["answer"])
    if kind == POLL_PENDING:
        return "не ответили на опрос или ещё не решили"
    if kind == CHECKED_IN:
        return f"отмечены в {day_label}"
    if kind == NOT_CHECKED_IN:
        return f"не отмечены в {day_label}"
    if kind == CONFIRMED_NOT_CHECKED_IN:
        return f"ответили «Приду», но не отмечены в {day_label}"
    if kind == TEAM:
        team = await session.get(Team, spec["team_id"])
        return f"команда «{team.name if team else '?'}»"
    if kind == SOLO:
        return "соло-участники"
    if kind == SELF:
        return "тест: только мне"
    return kind
