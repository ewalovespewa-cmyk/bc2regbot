"""Чекин: сканер в Mini App, «Позвать организатора», волонтёрский режим, управление днями, /stats."""
import html
from datetime import datetime, time
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.checkin_flow import handle_code
from app.bot.filters import RoleFilter
from app.bot.keyboards import main_menu, menu_text
from app.config import Settings
from app.db.models import Checkin, CheckinMethod, EventDay, Participant, Role, SecurityEvent
from app.i18n import t
from app.services import checkin_codes, ratelimit, security
from app.services.audit import log_admin_action
from app.services.checkin import (
    CheckinStatus,
    cancel_checkin,
    day_stats,
    is_open,
    manual_checkin,
    open_day,
    today_day,
    upsert_day_from_text,
)

router = Router(name="checkin")
router.message.filter(F.chat.type == "private")


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


# ---------------------------------------------------------------- участник


@router.message(F.web_app_data)
async def on_web_app_data(
    message: Message, state: FSMContext, session: AsyncSession, bot: Bot, redis: Redis, settings: Settings,
    participant: Participant | None, lang: str,
) -> None:
    """Mini App «Отметиться» отсканировал QR и вернул его текст через Telegram.WebApp.sendData."""
    code = checkin_codes.extract_code(message.web_app_data.data)
    await handle_code(message, code, state=state, session=session, bot=bot, redis=redis, settings=settings,
                      participant=participant, lang=lang)


@router.message(menu_text("checkin"))
async def on_checkin_button_without_webapp(message: Message, participant: Participant | None, lang: str) -> None:
    """Кнопка без Mini App (нет HTTPS-адреса): подсказываем сканировать камерой телефона."""
    await message.answer(t("checkin.scan_with_camera", participant.lang if participant else lang),
                         reply_markup=main_menu(participant.lang) if participant else None)


@router.callback_query(F.data == "ci:help")
async def on_call_organizer(
    callback: CallbackQuery, session: AsyncSession, bot: Bot, redis: Redis, settings: Settings,
    participant: Participant | None, lang: str,
) -> None:
    user = callback.from_user
    if not await ratelimit.hit(redis, f"help:{user.id}", 3, 600):
        await callback.answer(t("checkin.called", lang), show_alert=True)
        return
    who = participant.full_name if participant else (user.full_name or "")
    await security.report(
        session, bot=bot, redis=redis, settings=settings, kind=security.HELP_AT_ENTRANCE,
        telegram_user_id=user.id, username=user.username, participant_id=participant.id if participant else None,
        name=who, linked="да" if participant else "нет (номер не совпал с заявкой)",
    )
    await callback.message.answer(t("checkin.called", participant.lang if participant else lang))
    await callback.answer()


# ---------------------------------------------------------------- волонтёр: из карточки участника


def checkin_card_rows(p: Participant, role: Role, day: EventDay | None, checked: bool) -> list[list[InlineKeyboardButton]]:
    if day is None:
        return []
    if not checked:
        return [[_btn(f"✅ Отметить: {day.label}", f"ci:mark:{p.id}")]]
    if role in (Role.ADMIN, Role.SUPERADMIN):
        return [[_btn(f"↩️ Отменить чекин: {day.label}", f"ci:undo:{p.id}")]]
    return []


@router.callback_query(F.data.startswith("ci:mark:"), RoleFilter(Role.VOLUNTEER))
async def on_mark(callback: CallbackQuery, session: AsyncSession, settings: Settings, role: Role) -> None:
    p = await session.get(Participant, int(callback.data.split(":")[2]))
    day = await today_day(session, settings)
    if p is None or day is None:
        await callback.answer("Сегодня нет дня мероприятия", show_alert=True)
        return
    method = CheckinMethod.VOLUNTEER if role == Role.VOLUNTEER else CheckinMethod.ADMIN
    result = await manual_checkin(session, p, day, method, callback.from_user.id)
    await log_admin_action(session, callback.from_user.id, "checkin_manual", f"participant:{p.id}", day=day.label)
    if result.status == CheckinStatus.ALREADY:
        at = result.checkin.created_at.astimezone(ZoneInfo(settings.timezone)).strftime("%H:%M")
        await callback.answer(f"Уже отмечен в {at}", show_alert=True)
        return
    team = f" · команда {result.team_present}/{result.team_total}" if p.team and not p.team.is_solo else ""
    await callback.message.answer(f"✅ {html.escape(p.full_name)} отмечен(а): {html.escape(day.label)}{team}")
    await callback.answer()


@router.callback_query(F.data.startswith("ci:undo:"), RoleFilter(Role.ADMIN))
async def on_undo(callback: CallbackQuery, session: AsyncSession, settings: Settings) -> None:
    p = await session.get(Participant, int(callback.data.split(":")[2]))
    day = await today_day(session, settings)
    if p is None or day is None or not await cancel_checkin(session, p, day):
        await callback.answer("Чекина нет", show_alert=True)
        return
    await log_admin_action(session, callback.from_user.id, "checkin_cancel", f"participant:{p.id}", day=day.label)
    await callback.message.answer(f"↩️ Чекин {html.escape(p.full_name)} за {html.escape(day.label)} отменён (записано в журнал).")
    await callback.answer()


# ---------------------------------------------------------------- админ: дни, открыть/закрыть, экран


def _fmt_day(day: EventDay, tz: ZoneInfo) -> str:
    state = "🟢 открыт" if is_open(day) else ("🟡 включён, вне окна" if day.is_checkin_enabled else "⚪ закрыт")
    screen = "" if day.is_screen_enabled else " · экран выключен"
    return (
        f"{day.date:%d.%m} «{html.escape(day.label)}»: окно {day.checkin_opens_at.astimezone(tz):%H:%M}–"
        f"{day.checkin_closes_at.astimezone(tz):%H:%M}, {state}{screen}"
    )


@router.message(Command("days"), RoleFilter(Role.ADMIN))
async def cmd_days(message: Message, session: AsyncSession, settings: Settings) -> None:
    tz = ZoneInfo(settings.timezone)
    days = (await session.scalars(select(EventDay).order_by(EventDay.date))).all()
    lines = ["📅 <b>Дни мероприятия</b>"] + ([_fmt_day(d, tz) for d in days] or ["—"])
    lines.append(
        "\nИзменить: <code>/day 14.11 08:00-13:00 День 1</code> (суперадмин)\n"
        "Открыть/закрыть сегодня: /checkin_open, /checkin_close\nЭкран: <code>/screen on|off</code>"
    )
    await message.answer("\n".join(lines))


@router.message(Command("day"), RoleFilter(Role.SUPERADMIN))
async def cmd_day(message: Message, command: CommandObject, session: AsyncSession, settings: Settings) -> None:
    try:
        day = await upsert_day_from_text(session, settings, command.args or "")
    except ValueError as exc:
        await message.answer(f"{html.escape(str(exc))}\nНапример: <code>/day 14.11 08:00-13:00 День 1</code>")
        return
    await log_admin_action(session, message.from_user.id, "day_set", day.date.isoformat(), text=command.args)
    await message.answer("✅ " + _fmt_day(day, ZoneInfo(settings.timezone)))


@router.message(Command("checkin_open"), RoleFilter(Role.ADMIN))
async def cmd_checkin_open(message: Message, session: AsyncSession, settings: Settings) -> None:
    day = await today_day(session, settings)
    if day is None:
        await message.answer("Сегодня нет дня мероприятия. Список: /days")
        return
    if not settings.checkin_secret:
        await message.answer("⚠️ Не задан CHECKIN_SECRET — коды QR не будут работать.")
        return
    await open_day(session, day)
    await log_admin_action(session, message.from_user.id, "checkin_open", day.date.isoformat())
    await message.answer(
        f"🟢 Чекин открыт: {_fmt_day(day, ZoneInfo(settings.timezone))}\nЭкран: {_screen_url(settings)}",
        reply_markup=screen_links_keyboard(settings),
    )


@router.message(Command("checkin_close"), RoleFilter(Role.ADMIN))
async def cmd_checkin_close(message: Message, session: AsyncSession, settings: Settings) -> None:
    day = await today_day(session, settings)
    if day is None:
        await message.answer("Сегодня нет дня мероприятия.")
        return
    day.is_checkin_enabled = False
    await log_admin_action(session, message.from_user.id, "checkin_close", day.date.isoformat())
    await message.answer(f"⚪ Чекин закрыт: {html.escape(day.label)}. Волонтёры по-прежнему могут отмечать вручную.")


@router.message(Command("screen"), RoleFilter(Role.ADMIN))
async def cmd_screen(message: Message, command: CommandObject, session: AsyncSession, settings: Settings) -> None:
    arg = (command.args or "").strip().lower()
    day = await today_day(session, settings)
    if arg in {"on", "off"} and day is not None:
        day.is_screen_enabled = arg == "on"
        await log_admin_action(session, message.from_user.id, "screen", arg)
    state = "—" if day is None else ("включён" if day.is_screen_enabled else "выключен")
    await message.answer(
        f"🖥 Экран чекина: {state}\n"
        "Открой на ноутбуке у входа и разверни на весь экран (F11). Ссылки ниже — не публикуй их: "
        "с экраном QR можно отметиться из любой точки.\n\n"
        f"Экран: {_screen_url(settings)}\nДашборд: {_dashboard_url(settings)}",
        reply_markup=screen_links_keyboard(settings),
    )


def screen_links_keyboard(settings: Settings) -> InlineKeyboardMarkup | None:
    """Кнопки «открыть экран QR» и «открыть дашборд» — ссылка с ключом, копировать ничего не нужно."""
    row = []
    if settings.webhook_base_url and settings.screen_secret:
        row.append(InlineKeyboardButton(text="🖥 Открыть экран QR", url=_screen_url(settings)))
    if settings.webhook_base_url and settings.dashboard_secret:
        row.append(InlineKeyboardButton(text="📊 Дашборд", url=_dashboard_url(settings)))
    return InlineKeyboardMarkup(inline_keyboard=[row]) if row else None


def _screen_url(settings: Settings) -> str:
    if not settings.webhook_base_url or not settings.screen_secret:
        return "(нужны WEBHOOK_BASE_URL и SCREEN_SECRET)"
    return f"{settings.webhook_base_url}/checkin-screen?key={settings.screen_secret}"


def _dashboard_url(settings: Settings) -> str:
    if not settings.webhook_base_url or not settings.dashboard_secret:
        return "(нужны WEBHOOK_BASE_URL и DASHBOARD_SECRET)"
    return f"{settings.webhook_base_url}/dashboard?key={settings.dashboard_secret}"


# ---------------------------------------------------------------- статистика и подозрительное


@router.message(Command("stats"), RoleFilter(Role.ADMIN))
async def cmd_stats(message: Message, session: AsyncSession, settings: Settings) -> None:
    day = await today_day(session, settings) or await session.scalar(select(EventDay).order_by(EventDay.date.desc()))
    if day is None:
        await message.answer("Дней мероприятия нет. /days")
        return
    s = await day_stats(session, day)

    def block(title: str, items: list[str]) -> str:
        shown = ", ".join(html.escape(i) for i in items[:40]) or "—"
        more = f" … и ещё {len(items) - 40}" if len(items) > 40 else ""
        return f"<b>{title}</b> ({len(items)}): {shown}{more}"

    pct = round(100 * s.checked / s.total) if s.total else 0
    await message.answer(
        "\n".join(
            [
                f"📊 <b>{html.escape(day.label)}</b>: отмечено <b>{s.checked}</b> из {s.total} ({pct}%)",
                f"Соло: {s.solo_checked}/{s.solo_total}",
                "",
                block("✅ Команды в полном составе", s.teams_full),
                block("🟡 Частично", s.teams_partial),
                block("⚪ Никого", s.teams_none),
                f"\nДашборд: {_dashboard_url(settings)}",
            ]
        )[:4096]
    )


@router.message(Command("suspicious"), RoleFilter(Role.ADMIN))
async def cmd_suspicious(message: Message, session: AsyncSession, settings: Settings) -> None:
    tz = ZoneInfo(settings.timezone)
    events = (await session.scalars(select(SecurityEvent).order_by(SecurityEvent.id.desc()).limit(25))).all()
    if not events:
        await message.answer("Подозрительных событий нет 👌")
        return
    lines = ["🚨 <b>Последние подозрительные события</b>"]
    for e in events:
        title = security.TITLES.get(e.kind, e.kind)
        lines.append(f"{e.created_at.astimezone(tz):%d.%m %H:%M} · {html.escape(title)} · <code>{e.telegram_user_id}</code>")
    await message.answer("\n".join(lines)[:4096])


async def checked_today(session: AsyncSession, participant_id: int, day: EventDay | None) -> bool:
    if day is None:
        return False
    return bool(await session.scalar(select(Checkin.id).where(Checkin.participant_id == participant_id, Checkin.event_day_id == day.id)))
