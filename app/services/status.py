"""Экран «Мой статус»: имя, команда и состав, ответ на опрос явки, чекины по дням."""
import html
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Checkin, EventDay, Participant, Poll, PollAnswer, PollKind, PollOption
from app.i18n import t
from app.services.importer import team_display_name


async def attendance_answer(session: AsyncSession, participant_id: int, lang: str) -> str | None:
    row = await session.execute(
        select(PollOption.label_ru, PollOption.label_en)
        .join(PollAnswer, PollAnswer.option_id == PollOption.id)
        .join(Poll, Poll.id == PollAnswer.poll_id)
        .where(Poll.kind == PollKind.ATTENDANCE, PollAnswer.participant_id == participant_id)
        .order_by(Poll.created_at.desc())
        .limit(1)
    )
    found = row.first()
    if not found:
        return None
    label_ru, label_en = found
    return label_en if lang == "en" and label_en else label_ru


async def build_status(session: AsyncSession, participant: Participant, lang: str, timezone: str) -> str:
    tz = ZoneInfo(timezone)
    lines = [t("status.title", lang), t("status.name", lang, name=html.escape(participant.full_name))]

    team = participant.team
    if team is None or team.is_solo:
        lines.append(t("status.solo", lang))
    else:
        members = (
            await session.scalars(
                select(Participant)
                .where(Participant.team_id == team.id, Participant.is_selected)
                .order_by(Participant.full_name)
            )
        ).all()
        lines.append(t("status.team", lang, team=html.escape(team_display_name(team, lang))))
        for m in members:
            mark = "✅" if m.telegram_user_id else "⏳"
            you = t("status.you", lang) if m.id == participant.id else ""
            lines.append(f"  {mark} {html.escape(m.full_name)}{you}")
        lines.append(t("status.team_legend", lang))

    answer = await attendance_answer(session, participant.id, lang)
    lines.append("")
    lines.append(t("status.attendance", lang, answer=html.escape(answer)) if answer else t("status.attendance_none", lang))

    days = (await session.scalars(select(EventDay).order_by(EventDay.date))).all()
    if days:
        checkins = {
            c.event_day_id: c.created_at
            for c in (await session.scalars(select(Checkin).where(Checkin.participant_id == participant.id))).all()
        }
        lines.append(t("status.checkins", lang))
        for day in days:
            at = checkins.get(day.id)
            when = t("status.checked_at", lang, time=at.astimezone(tz).strftime("%H:%M")) if at else t("status.not_checked", lang)
            lines.append(f"  {html.escape(day.label)}: {when}")
    return "\n".join(lines)
