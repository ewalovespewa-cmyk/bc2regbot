"""Чекин на мероприятии: самочекин по QR, ручной (волонтёр/админ), отмена, статистика."""
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import Checkin, CheckinMethod, EventDay, Participant, Team
from app.services import checkin_codes, outbox
from app.services.importer import team_display_name


class CheckinStatus(StrEnum):
    OK = "ok"
    ALREADY = "already"
    INVALID_CODE = "invalid_code"
    CLOSED = "closed"


@dataclass
class CheckinResult:
    status: CheckinStatus
    day: EventDay | None = None
    checkin: Checkin | None = None
    team_present: int = 0
    team_total: int = 0


# ---------------------------------------------------------------- дни


def local_today(settings: Settings, now: datetime | None = None) -> date:
    return (now or datetime.now(UTC)).astimezone(ZoneInfo(settings.timezone)).date()


async def seed_event_days(session: AsyncSession, settings: Settings) -> int:
    """Создаёт дни из EVENT_DAYS, которых ещё нет в БД. Существующие не трогает."""
    tz = ZoneInfo(settings.timezone)
    created = 0
    for item in settings.event_days:
        day_date = date.fromisoformat(item["date"])
        if await session.scalar(select(EventDay).where(EventDay.date == day_date)):
            continue
        opens = datetime.combine(day_date, time.fromisoformat(item.get("opens", "08:00")), tz)
        closes = datetime.combine(day_date, time.fromisoformat(item.get("closes", "13:00")), tz)
        session.add(EventDay(date=day_date, label=item.get("label", str(day_date)), checkin_opens_at=opens, checkin_closes_at=closes))
        created += 1
    await session.flush()
    return created


async def today_day(session: AsyncSession, settings: Settings, now: datetime | None = None) -> EventDay | None:
    return await session.scalar(select(EventDay).where(EventDay.date == local_today(settings, now)))


def is_open(day: EventDay | None, now: datetime | None = None) -> bool:
    now = now or datetime.now(UTC)
    return bool(day and day.is_checkin_enabled and day.checkin_opens_at <= now <= day.checkin_closes_at)


async def open_day(session: AsyncSession, day: EventDay, now: datetime | None = None) -> None:
    """Открывает чекин дня прямо сейчас: включает флаг и при необходимости сдвигает окно."""
    now = now or datetime.now(UTC)
    day.is_checkin_enabled = True
    if day.checkin_opens_at > now:
        day.checkin_opens_at = now
    if day.checkin_closes_at < now + timedelta(hours=1):
        day.checkin_closes_at = now + timedelta(hours=4)
    await session.flush()


def day_number(day: EventDay, days: list[EventDay]) -> int:
    return next((i for i, d in enumerate(sorted(days, key=lambda d: d.date), start=1) if d.id == day.id), 1)


# ---------------------------------------------------------------- отметка


async def team_presence(session: AsyncSession, participant: Participant, day_id: int) -> tuple[int, int]:
    if participant.team_id is None:
        return (1, 1)
    members = select(Participant.id).where(Participant.team_id == participant.team_id, Participant.is_selected)
    total = await session.scalar(select(func.count()).select_from(members.subquery())) or 0
    present = await session.scalar(
        select(func.count()).select_from(Checkin).where(Checkin.event_day_id == day_id, Checkin.participant_id.in_(members))
    ) or 0
    return present, total


async def record_checkin(
    session: AsyncSession,
    participant: Participant,
    day: EventDay,
    method: CheckinMethod,
    checked_by: int | None = None,
) -> tuple[Checkin, bool]:
    """Идемпотентно: при гонке двух запросов создаётся ровно одна запись. (запись, создана ли сейчас)."""
    stmt = (
        insert(Checkin)
        .values(participant_id=participant.id, event_day_id=day.id, method=method, checked_by=checked_by)
        .on_conflict_do_nothing(index_elements=[Checkin.participant_id, Checkin.event_day_id])
        .returning(Checkin.id)
    )
    new_id = await session.scalar(stmt)
    checkin = await session.scalar(
        select(Checkin).where(Checkin.participant_id == participant.id, Checkin.event_day_id == day.id)
    )
    if new_id is not None:
        outbox.enqueue(
            session,
            outbox.CHECKIN_CREATED,
            {
                "telegram_user_id": participant.telegram_user_id,
                "participant_id": participant.id,
                "team": team_display_name(participant.team, "en"),
                "day": day.date.isoformat(),
                "day_label": day.label,
                "method": method.value,
                "timestamp": checkin.created_at.isoformat(),
            },
        )
        await session.flush()
    return checkin, new_id is not None


async def self_checkin(
    session: AsyncSession, participant: Participant, code: str, settings: Settings, now: datetime | None = None
) -> CheckinResult:
    now = now or datetime.now(UTC)
    day = await today_day(session, settings, now)
    if day is None or not is_open(day, now):
        # Проверяем код, только если чекин открыт — иначе «закрыт» важнее «устарел».
        return CheckinResult(CheckinStatus.CLOSED, day)
    if not checkin_codes.is_valid(settings.checkin_secret, day.id, code, now.timestamp()):
        return CheckinResult(CheckinStatus.INVALID_CODE, day)
    return await _finish(session, participant, day, CheckinMethod.SELF_QR, None)


async def checkin_pending(
    session: AsyncSession, participant: Participant, day_id: int, settings: Settings, now: datetime | None = None
) -> CheckinResult:
    """Отложенный чекин: код проверили при сканировании, человек привязал аккаунт позже."""
    day = await session.get(EventDay, day_id)
    if day is None or not is_open(day, now):
        return CheckinResult(CheckinStatus.CLOSED, day)
    return await _finish(session, participant, day, CheckinMethod.SELF_QR, None)


async def manual_checkin(
    session: AsyncSession, participant: Participant, day: EventDay, method: CheckinMethod, checked_by: int
) -> CheckinResult:
    return await _finish(session, participant, day, method, checked_by)


async def _finish(session, participant, day, method, checked_by) -> CheckinResult:
    checkin, created = await record_checkin(session, participant, day, method, checked_by)
    present, total = await team_presence(session, participant, day.id)
    return CheckinResult(CheckinStatus.OK if created else CheckinStatus.ALREADY, day, checkin, present, total)


async def cancel_checkin(session: AsyncSession, participant: Participant, day: EventDay) -> bool:
    result = await session.execute(
        delete(Checkin).where(Checkin.participant_id == participant.id, Checkin.event_day_id == day.id).returning(Checkin.id)
    )
    if result.first() is None:
        return False
    outbox.enqueue(
        session,
        outbox.CHECKIN_CANCELLED,
        {"telegram_user_id": participant.telegram_user_id, "participant_id": participant.id, "day": day.date.isoformat()},
    )
    await session.flush()
    return True


# ---------------------------------------------------------------- статистика


@dataclass
class DayStats:
    day: EventDay
    checked: int
    total: int
    teams_full: list[str] = field(default_factory=list)
    teams_partial: list[str] = field(default_factory=list)
    teams_none: list[str] = field(default_factory=list)
    solo_checked: int = 0
    solo_total: int = 0


async def day_stats(session: AsyncSession, day: EventDay) -> DayStats:
    total = await session.scalar(select(func.count()).select_from(Participant).where(Participant.is_selected)) or 0
    checked_ids = set((await session.scalars(select(Checkin.participant_id).where(Checkin.event_day_id == day.id))).all())
    stats = DayStats(day, len(checked_ids), total)
    rows = await session.execute(
        select(Team.name, Team.is_solo, Participant.id)
        .join(Participant, Participant.team_id == Team.id)
        .where(Participant.is_selected)
        .order_by(Team.name)
    )
    teams: dict[str, list[bool]] = {}
    for name, is_solo, pid in rows:
        if is_solo:
            stats.solo_total += 1
            stats.solo_checked += pid in checked_ids
        else:
            teams.setdefault(name, []).append(pid in checked_ids)
    for name, marks in teams.items():
        label = f"{name} ({sum(marks)}/{len(marks)})"
        if all(marks):
            stats.teams_full.append(label)
        elif any(marks):
            stats.teams_partial.append(label)
        else:
            stats.teams_none.append(label)
    return stats


async def checkins_timeline(session: AsyncSession, day: EventDay, bucket_minutes: int = 10) -> list[tuple[str, int]]:
    """Сколько отметилось по интервалам (для графика на дашборде), время — UTC ISO."""
    rows = (await session.scalars(select(Checkin.created_at).where(Checkin.event_day_id == day.id).order_by(Checkin.created_at))).all()
    buckets: dict[datetime, int] = {}
    for ts in rows:
        key = ts.replace(minute=ts.minute - ts.minute % bucket_minutes, second=0, microsecond=0)
        buckets[key] = buckets.get(key, 0) + 1
    return [(k.isoformat(), v) for k, v in sorted(buckets.items())]


# ---------------------------------------------------------------- сброс (для тестов)


async def reset_checkins(session: AsyncSession, day_id: int | None = None) -> int:
    """Удаляет отметки (одного дня или все). Бот игр получает checkin.cancelled по каждой —
    у тестировщиков снова закроется бинго. Возвращает, сколько отметок удалено."""
    stmt = select(Checkin, Participant, EventDay).join(Participant, Participant.id == Checkin.participant_id).join(
        EventDay, EventDay.id == Checkin.event_day_id
    )
    if day_id is not None:
        stmt = stmt.where(Checkin.event_day_id == day_id)
    rows = (await session.execute(stmt)).all()
    for checkin, participant, day in rows:
        outbox.enqueue(
            session,
            outbox.CHECKIN_CANCELLED,
            {"telegram_user_id": participant.telegram_user_id, "participant_id": participant.id, "day": day.date.isoformat()},
        )
        await session.delete(checkin)
    await session.flush()
    return len(rows)


async def delete_day(session: AsyncSession, day: EventDay) -> int:
    """Удаляет день (например, тестовый) вместе с его отметками. Возвращает число удалённых отметок."""
    removed = await reset_checkins(session, day.id)
    await session.delete(day)
    await session.flush()
    return removed


async def checkins_count(session: AsyncSession, day_id: int | None = None) -> int:
    stmt = select(func.count()).select_from(Checkin)
    if day_id is not None:
        stmt = stmt.where(Checkin.event_day_id == day_id)
    return await session.scalar(stmt) or 0


async def upsert_day_from_text(session: AsyncSession, settings: Settings, text: str) -> EventDay:
    """«14.11 08:00-13:00 День 1» → создать день или поправить окно и название. ValueError, если формат не тот."""
    parts = (text or "").split(maxsplit=2)
    tz = ZoneInfo(settings.timezone)
    try:
        day_date = datetime.strptime(parts[0], "%d.%m").replace(year=datetime.now(tz).year).date()
        start_s, end_s = parts[1].split("-")
        opens = datetime.combine(day_date, time.fromisoformat(start_s.strip()), tz)
        closes = datetime.combine(day_date, time.fromisoformat(end_s.strip()), tz)
    except (ValueError, IndexError) as exc:
        raise ValueError("Формат: 14.11 08:00-13:00 День 1") from exc
    if closes <= opens:
        raise ValueError("Конец окна должен быть позже начала")
    day = await session.scalar(select(EventDay).where(EventDay.date == day_date))
    if day is None:
        label = parts[2] if len(parts) > 2 else f"{day_date:%d.%m}"
        day = EventDay(date=day_date, label=label, checkin_opens_at=opens, checkin_closes_at=closes)
        session.add(day)
    else:
        day.checkin_opens_at, day.checkin_closes_at = opens, closes
        if len(parts) > 2:
            day.label = parts[2]
    await session.flush()
    return day
