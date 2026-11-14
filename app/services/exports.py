"""Выгрузки по вкладкам: «Статус бота» (все отобранные и их статусы) и по вкладке
на каждый день чекина. Одни и те же таблицы пишутся в Google Sheet и в Excel-файл.
"""
import io
import re
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Checkin, CheckinMethod, EventDay, Participant, Poll, PollAnswer, PollKind, PollOption
from app.services.checkin import is_test_day
from app.services.importer import participant_team_name

YES, NO = "да", "нет"
ROW_COLORS = {"yes": "C6EFCE", "no": "FFC7CE", "maybe": "FFEB9C"}  # ответ на опрос → заливка строки
METHOD_TITLES = {CheckinMethod.SELF_QR: "QR", CheckinMethod.VOLUNTEER: "волонтёр", CheckinMethod.ADMIN: "админ"}


@dataclass
class Table:
    name: str
    header: list[str]
    rows: list[list[str]]
    row_colors: list[str | None] | None = None  # цвет каждой строки (hex); None — таблицу не красим


def _fmt(dt: datetime | None, tz: ZoneInfo, pattern: str = "%d.%m %H:%M") -> str:
    return dt.astimezone(tz).strftime(pattern) if dt else ""


def checkin_tab_name(day: EventDay) -> str:
    # Google Sheets не любит в названиях листов []:*?/\ и длину > 100. Суффикс (BOT) — вкладка принадлежит боту.
    return re.sub(r"[\[\]:*?/\\]", " ", f"Чекин {day.label}")[:94] + " (BOT)"


async def _attendance(session: AsyncSession) -> dict[int, tuple[str, str | None, str, str]]:
    poll = await session.scalar(
        select(Poll).where(Poll.kind == PollKind.ATTENDANCE).order_by(Poll.created_at.desc()).limit(1)
    )
    if poll is None:
        return {}
    rows = await session.execute(
        select(PollAnswer.participant_id, PollOption.label_ru, PollAnswer.reason, PollAnswer.source, PollOption.code)
        .join(PollOption, PollOption.id == PollAnswer.option_id)
        .where(PollAnswer.poll_id == poll.id)
    )
    return {pid: (label, reason, source, code) for pid, label, reason, source, code in rows}


async def passed_table(session: AsyncSession, timezone: str, name: str = "Статус бота (BOT)") -> Table:
    """Вкладка «Статус бота»: строка = отобранный участник, цвет строки = ответ на опрос."""
    tz = ZoneInfo(timezone)
    days = [d for d in (await session.scalars(select(EventDay).order_by(EventDay.date))).all() if not is_test_day(d)]
    answers = await _attendance(session)
    checkins = {(c.participant_id, c.event_day_id): c.created_at for c in (await session.scalars(select(Checkin))).all()}
    participants = (
        await session.scalars(select(Participant).where(Participant.is_selected).order_by(Participant.id))
    ).all()

    header = [
        "ID заявки", "Имя", "Почта", "Телефон", "Telegram (форма)", "Команда", "Роль", "Вуз",
        "Открыл бота", "Telegram в боте", "Заблокировал бота", "Придёт?", "Причина", "Финал",
        *[f"Чекин: {d.label}" for d in days],
    ]
    rows = []
    colors = []
    for p in participants:
        answer = answers.get(p.id)
        colors.append(ROW_COLORS.get(answer[3]) if answer else None)
        answer_text = ""
        if answer:
            answer_text = answer[0] + (" (обзвон)" if answer[2] == "call" else "")
        rows.append(
            [
                p.application_id or "",
                p.full_name,
                p.email,
                p.phone or p.phone_raw or "",
                f"@{p.tg_username_form}" if p.tg_username_form else "",
                participant_team_name(p),
                p.team_role or "",
                p.university or "",
                YES if p.telegram_user_id else NO,
                f"@{p.telegram_username}" if p.telegram_username else ("" if not p.telegram_user_id else str(p.telegram_user_id)),
                YES if p.is_blocked_bot else "",
                answer_text,
                (answer[1] or "") if answer else "",
                YES if p.is_finalist else "",
                *[_fmt(checkins.get((p.id, d.id)), tz, "%H:%M") for d in days],
            ]
        )
    return Table(name, header, rows, row_colors=colors)


async def checkin_table(session: AsyncSession, day: EventDay, timezone: str) -> Table:
    """Вкладка дня: кто отметился и когда, по времени."""
    tz = ZoneInfo(timezone)
    result = await session.execute(
        select(Checkin, Participant)
        .join(Participant, Participant.id == Checkin.participant_id)
        .where(Checkin.event_day_id == day.id)
        .order_by(Checkin.created_at)
    )
    header = ["Время", "Имя", "Команда", "Телефон", "Почта", "Telegram", "Способ", "Кто отметил (ID)"]
    rows = [
        [
            _fmt(c.created_at, tz, "%H:%M:%S"),
            p.full_name,
            participant_team_name(p),
            p.phone or p.phone_raw or "",
            p.email,
            f"@{p.telegram_username}" if p.telegram_username else "",
            METHOD_TITLES.get(c.method, c.method.value),
            str(c.checked_by) if c.checked_by else "",
        ]
        for c, p in result.all()
    ]
    return Table(checkin_tab_name(day), header, rows)


FINALISTS_COLORS = {"came": "C6EFCE", "outsider": "FFEB9C"}  # пришёл финалист / отмечен не финалист


def finalists_tab_name(day: EventDay | None) -> str:
    # Не начинается с «Чекин »: такие вкладки бот удаляет вместе с днями.
    label = re.sub(r"[\[\]:*?/\\]", " ", day.label) if day else ""
    return f"Финалисты · {label} (BOT)" if label else "Финалисты (BOT)"


async def finalists_table(session: AsyncSession, timezone: str, finals_day: str) -> Table:
    """Вкладка финала: все финалисты по командам, пришли ли в день финала. Внизу — отмеченные не финалисты."""
    tz = ZoneInfo(timezone)
    day = None
    if finals_day:
        day = await session.scalar(select(EventDay).where(EventDay.date == datetime.fromisoformat(finals_day).date()))
    checkins: dict[int, Checkin] = {}
    if day is not None:
        checkins = {c.participant_id: c for c in (await session.scalars(select(Checkin).where(Checkin.event_day_id == day.id))).all()}
    finalists = (
        await session.scalars(select(Participant).where(Participant.is_selected, Participant.is_finalist))
    ).all()
    finalists = sorted(finalists, key=lambda p: (participant_team_name(p).lower(), p.full_name.lower()))
    team_total: dict[str, int] = {}
    team_came: dict[str, int] = {}
    for p in finalists:
        team = participant_team_name(p)
        team_total[team] = team_total.get(team, 0) + 1
        team_came[team] = team_came.get(team, 0) + (p.id in checkins)

    header = ["Команда", "Имя", "Телефон", "Почта", "Telegram", "Открыл бота", "Пришёл", "Время", "Способ", "Команда на месте"]
    rows: list[list[str]] = []
    colors: list[str | None] = []

    def row(p: Participant, came: str, color: str | None, team_mark: str) -> None:
        c = checkins.get(p.id)
        rows.append([
            participant_team_name(p),
            p.full_name,
            p.phone or p.phone_raw or "",
            p.email,
            f"@{p.telegram_username}" if p.telegram_username else (f"@{p.tg_username_form}" if p.tg_username_form else ""),
            YES if p.telegram_user_id else NO,
            came,
            _fmt(c.created_at, tz, "%H:%M") if c else "",
            METHOD_TITLES.get(c.method, c.method.value) if c else "",
            team_mark,
        ])
        colors.append(color)

    for p in finalists:
        team = participant_team_name(p)
        came = p.id in checkins
        row(p, YES if came else "", FINALISTS_COLORS["came"] if came else None, f"{team_came[team]}/{team_total[team]}")
    finalist_ids = {p.id for p in finalists}
    outsiders = [pid for pid in checkins if pid not in finalist_ids]
    if outsiders:
        # Пока список финалистов пуст, «не финалист» — неправда: просто показываем, кто пришёл.
        label, color = ("да, НЕ финалист", FINALISTS_COLORS["outsider"]) if finalists else (YES, None)
        for p in (await session.scalars(select(Participant).where(Participant.id.in_(outsiders)))).all():
            row(p, label, color, "")
    if not finalists:
        rows.insert(0, ["Список финалистов пуст: впишите почты финалистов во вкладку «Финал (BOT)»", *[""] * (len(header) - 1)])
        colors.insert(0, None)
    return Table(finalists_tab_name(day), header, rows, row_colors=colors)


async def all_tables(
    session: AsyncSession, timezone: str, passed_name: str = "Статус бота (BOT)", finals_day: str = ""
) -> list[Table]:
    """«Статус бота», «Чекин <день>» на каждый настоящий день и вкладка финалистов.
    День финала отдельной вкладкой «Чекин …» не пишем: всех пришедших показывает вкладка финалистов.
    Тестовые дни («Тест…») в таблицу не попадают."""
    tables = [await passed_table(session, timezone, passed_name)]
    for day in (await session.scalars(select(EventDay).order_by(EventDay.date))).all():
        if is_test_day(day) or (finals_day and day.date.isoformat() == finals_day):
            continue
        tables.append(await checkin_table(session, day, timezone))
    if finals_day:
        tables.append(await finalists_table(session, timezone, finals_day))
    return tables


async def bot_status_table(session: AsyncSession, timezone: str) -> tuple[list[str], list[list[str]]]:
    """Вкладка «Статус бота» как (заголовок, строки) — для CSV."""
    table = await passed_table(session, timezone)
    return table.header, table.rows


def tables_to_xlsx(tables: list[Table]) -> bytes:
    """Excel-файл: по листу на таблицу, жирная шапка, закреплённая первая строка, ширина по содержимому."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill

    wb = Workbook()
    wb.remove(wb.active)
    head_font = Font(bold=True, color="FFFFFF")
    head_fill = PatternFill("solid", fgColor="5B42DC")
    for table in tables:
        ws = wb.create_sheet(table.name[:31])  # Excel ограничивает имя листа 31 символом
        ws.append(table.header)
        for cell in ws[1]:
            cell.font, cell.fill = head_font, head_fill
        for n, row in enumerate(table.rows):
            ws.append(row)
            if table.row_colors and table.row_colors[n]:
                for cell in ws[ws.max_row]:
                    cell.fill = PatternFill("solid", fgColor=table.row_colors[n])
        ws.freeze_panes = "A2"
        for column in ws.columns:
            width = max((len(str(c.value or "")) for c in column), default=8)
            ws.column_dimensions[column[0].column_letter].width = min(max(width + 2, 8), 60)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
