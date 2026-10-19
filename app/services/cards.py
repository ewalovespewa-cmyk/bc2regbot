"""Карточка участника для админов и волонтёров."""
import html
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Checkin, EventDay, Participant, Role
from app.services.importer import team_display_name
from app.services.roles import has_role
from app.services.status import attendance_answer


def short_line(p: Participant) -> str:
    team = team_display_name(p.team)
    return f"{p.full_name} · {team}"


async def participant_card(session: AsyncSession, p: Participant, role: Role | None, timezone: str) -> str:
    tz = ZoneInfo(timezone)
    esc = html.escape
    lines = [f"🪪 <b>{esc(p.full_name)}</b>", f"Команда: {esc(team_display_name(p.team))}"]
    if p.team and not p.team.is_solo:
        mates = (
            await session.scalars(
                select(Participant.full_name).where(
                    Participant.team_id == p.team_id, Participant.id != p.id, Participant.is_selected
                )
            )
        ).all()
        if mates:
            lines.append("Состав: " + ", ".join(esc(m) for m in mates))
    lines.append(f"Телефон: {esc(p.phone or p.phone_raw or '—')}")

    if has_role(role, Role.ADMIN):
        lines.append(f"Email: {esc(p.email)}")
        if p.university:
            lines.append(f"Вуз: {esc(p.university)}")

    if p.telegram_user_id:
        who = f"@{esc(p.telegram_username)}" if p.telegram_username else f"id {p.telegram_user_id}"
        linked = p.linked_at.astimezone(tz).strftime("%d.%m %H:%M") if p.linked_at else ""
        lines.append(f"Бот: ✅ {who} {linked}".rstrip())
        if p.is_blocked_bot:
            lines.append("⚠️ Заблокировал бота")
    else:
        lines.append("Бот: ⏳ ещё не открывал")

    answer = await attendance_answer(session, p.id, "ru")
    lines.append(f"Опрос «Придёшь?»: {esc(answer) if answer else '—'}")

    days = (await session.scalars(select(EventDay).order_by(EventDay.date))).all()
    if days:
        checkins = {
            c.event_day_id: c
            for c in (await session.scalars(select(Checkin).where(Checkin.participant_id == p.id))).all()
        }
        for day in days:
            c = checkins.get(day.id)
            mark = f"✅ {c.created_at.astimezone(tz):%H:%M} ({c.method.value})" if c else "—"
            lines.append(f"Чекин {esc(day.label)}: {mark}")
    return "\n".join(lines)
