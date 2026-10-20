"""Админка на кнопках: всё, что умеют команды, доступно из меню /admin без набора текста.

Разделы: 👥 Участники (импорт/выгрузка/синхронизация) · ✅ Чекин (открыть/закрыть, экран,
статистика, дни, сброс отметок) · 📣 Рассылки и опрос · 📝 Контент · 🔎 Поиск · ⚙️ Команда бота.
Команды (/import, /stats, …) по-прежнему работают — это тот же функционал.
"""
import html
import io
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.admin_commands import apply_admin_commands
from app.bot.filters import RoleFilter
from app.config import Settings
from app.db.models import EventDay, Role
from app.services import checkin as checkin_service
from app.services import polls as poll_service
from app.services.admins import ASSIGNABLE_ROLES, list_admins, remove_admin, resolve_user, set_admin
from app.services.audit import log_admin_action
from app.services.exports import all_tables, tables_to_xlsx
from app.services.importer import ImportAborted, assign_teams
from app.services.roles import has_role
from app.services.settings_store import AUTO_IMPORT_ENABLED, get_setting, set_setting
from app.services.sheet_sync import format_import_report, format_teams_report, import_from_sheet, import_rows, push_status_to_sheet
from app.services.sources import SheetsError, parse_table_file

router = Router(name="admin_ui")
router.message.filter(F.chat.type == "private")
router.callback_query.filter(F.message.chat.type == "private")

ROLE_TITLES = {Role.SUPERADMIN: "суперадмин", Role.ADMIN: "админ", Role.VOLUNTEER: "волонтёр"}
ADMIN_BUTTON = "🛠 Админка"
MAX_FILE_BYTES = 5 * 1024 * 1024


class AdminUI(StatesGroup):
    import_file = State()  # ждём CSV/XLSX
    day_text = State()  # «14.11 08:00-13:00 День 1»
    admin_ref = State()  # id или @username нового админа


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def _kb(*rows: list[InlineKeyboardButton]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[r for r in rows if r])


BACK = [_btn("« Главное меню", "m:main")]


def admin_reply_keyboard() -> ReplyKeyboardMarkup:
    """Постоянная кнопка «🛠 Админка» внизу — для админов, которые сами не участники."""
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=ADMIN_BUTTON)]], resize_keyboard=True, is_persistent=True)


async def _edit_or_send(event: Message | CallbackQuery, text: str, markup: InlineKeyboardMarkup | None) -> None:
    if isinstance(event, CallbackQuery):
        try:
            await event.message.edit_text(text, reply_markup=markup)
        except Exception:  # сообщение слишком старое или текст не изменился
            await event.message.answer(text, reply_markup=markup)
        await event.answer()
    else:
        await event.answer(text, reply_markup=markup)


# ---------------------------------------------------------------- главное меню


def main_menu_kb(role: Role, settings: Settings) -> InlineKeyboardMarkup:
    from app.bot.handlers.checkin import screen_links_keyboard

    rows: list[list[InlineKeyboardButton]] = []
    if has_role(role, Role.ADMIN):
        rows.append([_btn("👥 Участники", "m:people"), _btn("✅ Чекин", "m:checkin")])
        rows.append([_btn("📣 Рассылки и опрос", "m:comms"), _btn("📝 Контент", "m:content")])
    rows.append([_btn("🔎 Найти участника", "adm:find")])
    if has_role(role, Role.SUPERADMIN):
        rows.append([_btn("⚙️ Команда бота и журнал", "m:team")])
    if has_role(role, Role.ADMIN) and (links := screen_links_keyboard(settings)):
        rows.append(links.inline_keyboard[0])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def show_main(event: Message | CallbackQuery, role: Role, settings: Settings) -> None:
    await _edit_or_send(event, f"🛠 <b>Админка</b> · роль: {ROLE_TITLES[role]}", main_menu_kb(role, settings))


@router.message(Command("admin"), RoleFilter(Role.VOLUNTEER))
@router.message(F.text == ADMIN_BUTTON, RoleFilter(Role.VOLUNTEER))
async def cmd_admin(message: Message, bot: Bot, state: FSMContext, role: Role, settings: Settings) -> None:
    await state.clear()
    await apply_admin_commands(bot, message.from_user.id, role)
    await show_main(message, role, settings)


@router.callback_query(F.data == "m:main", RoleFilter(Role.VOLUNTEER))
async def on_main(callback: CallbackQuery, state: FSMContext, role: Role, settings: Settings) -> None:
    await state.clear()
    await show_main(callback, role, settings)


# ---------------------------------------------------------------- 👥 Участники


async def _people_text(session: AsyncSession, settings: Settings) -> str:
    from sqlalchemy import func

    from app.db.models import Participant

    total = await session.scalar(select(func.count()).select_from(Participant).where(Participant.is_selected)) or 0
    linked = await session.scalar(
        select(func.count()).select_from(Participant).where(Participant.is_selected, Participant.telegram_user_id.is_not(None))
    ) or 0
    auto = await get_setting(session, AUTO_IMPORT_ENABLED, False)
    sheet = "подключён" if settings.sheets_enabled else "не подключён — работаем через файлы"
    return (
        f"👥 <b>Участники</b>\nОтобрано: <b>{total}</b>, открыли бота: <b>{linked}</b>\n"
        f"Google Sheet: {sheet}" + (f"\nАвтоимпорт: {'вкл' if auto else 'выкл'}" if settings.sheets_enabled else "")
    )


@router.callback_query(F.data == "m:people", RoleFilter(Role.ADMIN))
async def on_people(callback: CallbackQuery, session: AsyncSession, settings: Settings, role: Role) -> None:
    rows = []
    if has_role(role, Role.SUPERADMIN):
        rows.append([_btn("📥 Импорт отобранных (файл)", "a:imp:selected")])
        rows.append([_btn("📥 Импорт всех из файла", "a:imp:all"), _btn("👥 Команды из файла", "a:imp:teams")])
        if settings.sheets_enabled:
            rows.append([_btn("🔄 Импорт из Google Sheet", "a:imp:sheet")])
    rows.append([_btn("📊 Выгрузить Excel", "a:export")])
    if settings.sheets_enabled:
        rows.append([_btn("🔄 Обновить Google Sheet сейчас", "a:sync")])
        if has_role(role, Role.SUPERADMIN):
            auto = await get_setting(session, AUTO_IMPORT_ENABLED, False)
            rows.append([_btn(f"🤖 Автоимпорт: {'выключить' if auto else 'включить'}", "a:autoimp")])
    rows.append(BACK)
    await _edit_or_send(callback, await _people_text(session, settings), _kb(*rows))


IMPORT_HINTS = {
    "selected": "Пришли таблицу кандидатов (CSV или Excel). Импортирую тех, у кого в колонке «Состояние» "
    "стоит «Прошёл отбор» (или Selected = yes).",
    "all": "Пришли файл, где <b>все</b> строки — отобранные участники (CSV или Excel). Колонка отбора не нужна.",
    "teams": "Пришли файл с колонками <code>team_name</code> и <code>email</code> (CSV или Excel).",
}


@router.callback_query(F.data.in_({"a:imp:selected", "a:imp:all", "a:imp:teams"}), RoleFilter(Role.SUPERADMIN))
async def on_import_ask(callback: CallbackQuery, state: FSMContext) -> None:
    mode = callback.data.split(":")[2]
    await state.set_state(AdminUI.import_file)
    await state.update_data(import_mode=mode)
    await callback.message.answer("📎 " + IMPORT_HINTS[mode], reply_markup=_kb([_btn("Отмена", "m:people")]))
    await callback.answer()


async def _run_import(
    message: Message, data: bytes, filename: str | None, mode: str, actor: int, session: AsyncSession, settings: Settings
) -> None:
    try:
        rows = parse_table_file(data, filename)
        if mode == "teams":
            report = await assign_teams(session, rows)
            text = format_teams_report(report)
        else:
            report = await import_rows(session, settings, rows, assume_selected=mode == "all")
            text = format_import_report(report)
    except (ImportAborted, ValueError) as exc:
        await session.rollback()
        await message.answer(f"❌ Ничего не записано.\n{html.escape(str(exc))}")
        return
    await log_admin_action(session, actor, f"import_{mode}", filename or "file")
    await session.commit()
    await message.answer(text.replace("Ссылки для рассылки: /export", "Ссылки для писем — в «📊 Выгрузить Excel»."),
                         reply_markup=_kb([_btn("👥 К участникам", "m:people")], BACK))


async def _download(bot: Bot, file_id: str, size: int | None) -> bytes:
    if size and size > MAX_FILE_BYTES:
        raise ValueError("Файл больше 5 МБ — это точно таблица?")
    buf = io.BytesIO()
    await bot.download(file_id, destination=buf, timeout=60)
    return buf.getvalue()


@router.message(AdminUI.import_file, F.document, RoleFilter(Role.SUPERADMIN))
async def on_import_file(message: Message, state: FSMContext, bot: Bot, session: AsyncSession, settings: Settings) -> None:
    mode = (await state.get_data()).get("import_mode", "selected")
    await state.clear()
    try:
        data = await _download(bot, message.document.file_id, message.document.file_size)
    except ValueError as exc:
        await message.answer(f"❌ {html.escape(str(exc))}")
        return
    await _run_import(message, data, message.document.file_name, mode, message.from_user.id, session, settings)


@router.message(StateFilter(None), F.document, ~F.caption.startswith("/"), RoleFilter(Role.SUPERADMIN))
async def on_any_file(message: Message, state: FSMContext) -> None:
    """Прислали файл без команды — спросим, что с ним сделать."""
    name = (message.document.file_name or "").lower()
    if not name.endswith((".csv", ".xlsx", ".xlsm", ".txt")):
        return
    await state.update_data(pending_file={"id": message.document.file_id, "name": message.document.file_name,
                                          "size": message.document.file_size})
    await message.reply(
        "Что сделать с этим файлом?",
        reply_markup=_kb(
            [_btn("📥 Импорт отобранных («Прошёл отбор»)", "a:file:selected")],
            [_btn("📥 Импорт всех строк", "a:file:all"), _btn("👥 Это команды", "a:file:teams")],
        ),
    )


@router.callback_query(F.data.startswith("a:file:"), RoleFilter(Role.SUPERADMIN))
async def on_pending_file(callback: CallbackQuery, state: FSMContext, bot: Bot, session: AsyncSession, settings: Settings) -> None:
    pending = (await state.get_data()).get("pending_file")
    if not pending:
        await callback.answer("Файл не найден — пришли его ещё раз", show_alert=True)
        return
    await callback.answer("Импортирую…")
    try:
        data = await _download(bot, pending["id"], pending.get("size"))
    except ValueError as exc:
        await callback.message.answer(f"❌ {html.escape(str(exc))}")
        return
    await state.update_data(pending_file=None)
    await _run_import(callback.message, data, pending.get("name"), callback.data.split(":")[2], callback.from_user.id,
                      session, settings)


@router.callback_query(F.data == "a:imp:sheet", RoleFilter(Role.SUPERADMIN))
async def on_import_sheet(callback: CallbackQuery, session: AsyncSession, settings: Settings) -> None:
    await callback.answer("Читаю Google Sheet…")
    try:
        report, teams_report = await import_from_sheet(session, settings)
    except (ImportAborted, SheetsError) as exc:
        await session.rollback()
        await callback.message.answer(f"❌ {html.escape(str(exc))}")
        return
    await log_admin_action(session, callback.from_user.id, "import_sheet", created=report.created)
    await session.commit()
    text = format_import_report(report) + ("\n\n" + format_teams_report(teams_report) if teams_report else "")
    await callback.message.answer(text, reply_markup=_kb(BACK))


@router.callback_query(F.data == "a:export", RoleFilter(Role.ADMIN))
async def on_export(callback: CallbackQuery, session: AsyncSession, settings: Settings) -> None:
    tables = await all_tables(session, settings.bot_username, settings.timezone, settings.sheet_status_worksheet)
    stamp = datetime.now(ZoneInfo(settings.timezone)).strftime("%Y%m%d_%H%M")
    tabs = ", ".join(f"«{t.name}» ({len(t.rows)})" for t in tables)
    await callback.message.answer_document(
        BufferedInputFile(tables_to_xlsx(tables), filename=f"business_cup_{stamp}.xlsx"),
        caption=f"📊 Листы: {tabs}.\nВ «{tables[0].name}» колонка «Ссылка-приглашение» — для писем.",
    )
    await log_admin_action(session, callback.from_user.id, "export_xlsx")
    await callback.answer()


@router.callback_query(F.data == "a:sync", RoleFilter(Role.ADMIN))
async def on_sync(callback: CallbackQuery, session: AsyncSession, settings: Settings) -> None:
    await callback.answer("Обновляю Google Sheet…")
    try:
        count = await push_status_to_sheet(session, settings)
    except SheetsError as exc:
        await callback.message.answer(f"❌ {html.escape(str(exc))}")
        return
    await callback.message.answer(f"✅ Google Sheet обновлён: {count} участников, вкладки по дням чекина тоже.")


@router.callback_query(F.data == "a:autoimp", RoleFilter(Role.SUPERADMIN))
async def on_autoimport(callback: CallbackQuery, session: AsyncSession, settings: Settings, role: Role) -> None:
    enabled = not await get_setting(session, AUTO_IMPORT_ENABLED, False)
    await set_setting(session, AUTO_IMPORT_ENABLED, enabled)
    await log_admin_action(session, callback.from_user.id, "autoimport", "on" if enabled else "off")
    await session.flush()
    await on_people(callback, session, settings, role)


# ---------------------------------------------------------------- ✅ Чекин


async def _checkin_text(session: AsyncSession, settings: Settings) -> tuple[str, EventDay | None]:
    from app.bot.handlers.checkin import _fmt_day

    tz = ZoneInfo(settings.timezone)
    day = await checkin_service.today_day(session, settings)
    if day is None:
        days = (await session.scalars(select(EventDay).order_by(EventDay.date))).all()
        listing = "\n".join(_fmt_day(d, tz) for d in days) or "—"
        return f"✅ <b>Чекин</b>\nСегодня не день мероприятия.\n\n{listing}", None
    stats = await checkin_service.day_stats(session, day)
    return (
        f"✅ <b>Чекин сегодня</b>\n{_fmt_day(day, tz)}\n"
        f"Отмечено: <b>{stats.checked}</b> из {stats.total} · команд в полном составе: {len(stats.teams_full)}",
        day,
    )


@router.callback_query(F.data == "m:checkin", RoleFilter(Role.ADMIN))
async def on_checkin(callback: CallbackQuery, session: AsyncSession, settings: Settings, role: Role) -> None:
    from app.bot.handlers.checkin import screen_links_keyboard

    text, day = await _checkin_text(session, settings)
    rows = []
    if day is not None:
        if checkin_service.is_open(day):
            rows.append([_btn("⚪ Закрыть чекин", "a:ci:close")])
        else:
            rows.append([_btn("🟢 Открыть чекин сейчас", "a:ci:open")])
        rows.append([_btn("🖥 Экран: " + ("выключить" if day.is_screen_enabled else "включить"), "a:ci:screen")])
    rows.append([_btn("📊 Статистика по командам", "a:ci:stats"), _btn("📅 Дни", "m:days")])
    if has_role(role, Role.SUPERADMIN):
        rows.append([_btn("🧹 Сбросить отметки (тест)", "m:reset")])
    if links := screen_links_keyboard(settings):
        rows.append(links.inline_keyboard[0])
    rows.append([_btn("🚨 Подозрительное", "a:susp"), _btn("🔄 Обновить", "m:checkin")])
    rows.append(BACK)
    await _edit_or_send(callback, text, _kb(*rows))


@router.callback_query(F.data.in_({"a:ci:open", "a:ci:close", "a:ci:screen"}), RoleFilter(Role.ADMIN))
async def on_checkin_action(callback: CallbackQuery, session: AsyncSession, settings: Settings, role: Role) -> None:
    day = await checkin_service.today_day(session, settings)
    if day is None:
        await callback.answer("Сегодня нет дня мероприятия — добавь его в «📅 Дни»", show_alert=True)
        return
    action = callback.data.split(":")[2]
    if action == "open":
        if not settings.checkin_secret:
            await callback.answer("Не задан CHECKIN_SECRET — коды QR не будут работать", show_alert=True)
            return
        await checkin_service.open_day(session, day)
    elif action == "close":
        day.is_checkin_enabled = False
    else:
        day.is_screen_enabled = not day.is_screen_enabled
    await log_admin_action(session, callback.from_user.id, f"checkin_{action}", day.date.isoformat())
    await session.flush()
    await on_checkin(callback, session, settings, role)


@router.callback_query(F.data == "a:ci:stats", RoleFilter(Role.ADMIN))
async def on_stats(callback: CallbackQuery, session: AsyncSession, settings: Settings) -> None:
    from app.bot.handlers.checkin import cmd_stats

    await cmd_stats(callback.message, session, settings)
    await callback.answer()


@router.callback_query(F.data == "a:susp", RoleFilter(Role.ADMIN))
async def on_suspicious(callback: CallbackQuery, session: AsyncSession, settings: Settings) -> None:
    from app.bot.handlers.checkin import cmd_suspicious

    await cmd_suspicious(callback.message, session, settings)
    await callback.answer()


# --- дни


@router.callback_query(F.data == "m:days", RoleFilter(Role.ADMIN))
async def on_days(callback: CallbackQuery, state: FSMContext, session: AsyncSession, settings: Settings, role: Role) -> None:
    from app.bot.handlers.checkin import _fmt_day

    await state.clear()
    tz = ZoneInfo(settings.timezone)
    days = (await session.scalars(select(EventDay).order_by(EventDay.date))).all()
    lines = ["📅 <b>Дни мероприятия</b>"] + ([_fmt_day(d, tz) for d in days] or ["—"])
    rows = []
    if has_role(role, Role.SUPERADMIN):
        for d in days:
            rows.append([_btn(f"✏️ {d.label} {d.date:%d.%m}", f"a:day:edit:{d.id}"), _btn("🗑 Удалить", f"a:day:del:{d.id}")])
        rows.append([_btn("➕ Добавить день", "a:day:add")])
    rows.append([_btn("« Чекин", "m:checkin")])
    await _edit_or_send(callback, "\n".join(lines), _kb(*rows))


@router.callback_query(F.data.startswith("a:day:edit:") | (F.data == "a:day:add"), RoleFilter(Role.SUPERADMIN))
async def on_day_edit(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    example = "14.11 08:00-13:00 День 1"
    if callback.data.startswith("a:day:edit:"):
        day = await session.get(EventDay, int(callback.data.split(":")[3]))
        if day:
            tz_hint = f"{day.date:%d.%m} 08:00-13:00 {day.label}"
            example = tz_hint
    await state.set_state(AdminUI.day_text)
    await callback.message.answer(
        f"Пришли дату, окно чекина и название одним сообщением, например:\n<code>{html.escape(example)}</code>\n"
        "Если дата уже есть — поменяю окно и название.",
        reply_markup=_kb([_btn("Отмена", "m:days")]),
    )
    await callback.answer()


@router.message(AdminUI.day_text, F.text, ~F.text.startswith("/"), RoleFilter(Role.SUPERADMIN))
async def on_day_text(message: Message, state: FSMContext, session: AsyncSession, settings: Settings) -> None:
    from app.bot.handlers.checkin import _fmt_day

    try:
        day = await checkin_service.upsert_day_from_text(session, settings, message.text)
    except ValueError as exc:
        await message.answer(f"❌ {html.escape(str(exc))}")
        return
    await state.clear()
    await log_admin_action(session, message.from_user.id, "day_set", day.date.isoformat(), text=message.text)
    await message.answer("✅ " + _fmt_day(day, ZoneInfo(settings.timezone)), reply_markup=_kb([_btn("📅 Дни", "m:days")], BACK))


@router.callback_query(F.data.startswith("a:day:del:"), RoleFilter(Role.SUPERADMIN))
async def on_day_delete_ask(callback: CallbackQuery, session: AsyncSession) -> None:
    day = await session.get(EventDay, int(callback.data.split(":")[3]))
    if day is None:
        await callback.answer("Уже удалён", show_alert=True)
        return
    count = await checkin_service.checkins_count(session, day.id)
    await _edit_or_send(
        callback,
        f"🗑 Удалить день «{html.escape(day.label)}» ({day.date:%d.%m})? Отметок в нём: {count} — они тоже удалятся.",
        _kb([_btn("Да, удалить", f"a:day:delok:{day.id}"), _btn("Отмена", "m:days")]),
    )


@router.callback_query(F.data.startswith("a:day:delok:"), RoleFilter(Role.SUPERADMIN))
async def on_day_delete(callback: CallbackQuery, state: FSMContext, session: AsyncSession, settings: Settings, role: Role) -> None:
    day = await session.get(EventDay, int(callback.data.split(":")[3]))
    if day is not None:
        label = day.label
        removed = await checkin_service.delete_day(session, day)
        await log_admin_action(session, callback.from_user.id, "day_delete", label, checkins=removed)
        await callback.message.answer(f"🗑 День «{html.escape(label)}» удалён, отметок удалено: {removed}.")
    await on_days(callback, state, session, settings, role)


# --- сброс отметок


@router.callback_query(F.data == "m:reset", RoleFilter(Role.SUPERADMIN))
async def on_reset_menu(callback: CallbackQuery, session: AsyncSession, settings: Settings) -> None:
    day = await checkin_service.today_day(session, settings)
    total = await checkin_service.checkins_count(session)
    rows = []
    if day is not None:
        rows.append([_btn(f"Сегодня — «{day.label}» ({await checkin_service.checkins_count(session, day.id)})", "a:reset:today")])
    rows.append([_btn(f"Все дни ({total})", "a:reset:all")])
    rows.append([_btn("« Чекин", "m:checkin")])
    await _edit_or_send(
        callback,
        "🧹 <b>Сброс отметок</b> — для тестов. Отметки удалятся, участники смогут отметиться заново, "
        "в боте игр у них снова закроется бинго. Привязки к боту и ответы на опрос не трогаются.",
        _kb(*rows),
    )


@router.callback_query(F.data.in_({"a:reset:today", "a:reset:all"}), RoleFilter(Role.SUPERADMIN))
async def on_reset_ask(callback: CallbackQuery) -> None:
    scope = callback.data.split(":")[2]
    what = "за сегодня" if scope == "today" else "за <b>все дни</b>"
    await _edit_or_send(
        callback,
        f"Точно удалить все отметки {what}? Это нельзя отменить.",
        _kb([_btn("Да, сбросить", f"a:resetok:{scope}"), _btn("Отмена", "m:reset")]),
    )


@router.callback_query(F.data.startswith("a:resetok:"), RoleFilter(Role.SUPERADMIN))
async def on_reset(callback: CallbackQuery, session: AsyncSession, settings: Settings) -> None:
    scope = callback.data.split(":")[2]
    day_id = None
    if scope == "today":
        day = await checkin_service.today_day(session, settings)
        if day is None:
            await callback.answer("Сегодня нет дня мероприятия", show_alert=True)
            return
        day_id = day.id
    removed = await checkin_service.reset_checkins(session, day_id)
    await log_admin_action(session, callback.from_user.id, "checkins_reset", scope, removed=removed)
    await _edit_or_send(callback, f"🧹 Сброшено отметок: <b>{removed}</b>.", _kb([_btn("« Чекин", "m:checkin")], BACK))


# ---------------------------------------------------------------- 📣 Рассылки и опрос


@router.callback_query(F.data == "m:comms", RoleFilter(Role.ADMIN))
async def on_comms(callback: CallbackQuery, session: AsyncSession) -> None:
    poll = await poll_service.latest_attendance_poll(session)
    poll_line = "Опрос явки ещё не запускали." if poll is None else (
        f"Опрос явки #{poll.id}: {'идёт' if poll_service.is_open(poll) else 'закрыт'}."
    )
    rows = [
        [_btn("➕ Новая рассылка", "bc:new"), _btn("📄 Последние рассылки", "a:bc:list")],
        [_btn("📋 Сводка опроса", "a:poll:summary")],
    ]
    if poll is not None and poll_service.is_open(poll):
        rows.append([_btn("⏹ Закрыть опрос", "a:poll:close")])
    else:
        rows.append([_btn("▶️ Запустить опрос «Придёшь?»", "a:poll:start")])
    rows.append([_btn("📞 Список на обзвон", "pa:calls"), _btn("👥 Опрос по командам", "pa:teams")])
    rows.append(BACK)
    await _edit_or_send(callback, f"📣 <b>Рассылки и опрос</b>\n{poll_line}", _kb(*rows))


@router.callback_query(F.data == "a:bc:list", RoleFilter(Role.ADMIN))
async def on_broadcast_list(callback: CallbackQuery, session: AsyncSession, settings: Settings) -> None:
    from app.bot.handlers.admin_broadcast import cmd_broadcasts

    await cmd_broadcasts(callback.message, session, settings)
    await callback.answer()


@router.callback_query(F.data == "a:poll:summary", RoleFilter(Role.ADMIN))
async def on_poll_summary(callback: CallbackQuery, session: AsyncSession) -> None:
    poll = await poll_service.latest_attendance_poll(session)
    if poll is None:
        await callback.answer("Опрос ещё не запускали", show_alert=True)
        return
    await callback.message.answer(
        await poll_service.poll_summary(session, poll),
        reply_markup=_kb([_btn("📞 Обзвон", "pa:calls"), _btn("👥 По командам", "pa:teams")], [_btn("« Назад", "m:comms")]),
    )
    await callback.answer()


@router.callback_query(F.data == "a:poll:start", RoleFilter(Role.ADMIN))
async def on_poll_start_ask(callback: CallbackQuery, settings: Settings) -> None:
    test = "\n🧪 TEST_MODE: опрос получат только админы." if settings.test_mode else ""
    await _edit_or_send(
        callback,
        "▶️ Разослать всем привязанным участникам опрос «Ты придёшь?» с кнопками «Приду / Не смогу / Пока не знаю»?\n"
        f"Тем, кто не ответил или ответил «не знаю», через 6 часов придёт одно напоминание.{test}",
        _kb([_btn("Да, запустить", "a:poll:startok"), _btn("Отмена", "m:comms")]),
    )


@router.callback_query(F.data == "a:poll:startok", RoleFilter(Role.ADMIN))
async def on_poll_start(callback: CallbackQuery, session: AsyncSession, settings: Settings) -> None:
    current = await poll_service.latest_attendance_poll(session)
    if current is not None and poll_service.is_open(current):
        await callback.answer("Опрос уже идёт", show_alert=True)
        return
    poll = await poll_service.create_attendance_poll(session, created_by=callback.from_user.id)
    b = await poll_service.launch_poll(session, poll, settings)
    await log_admin_action(session, callback.from_user.id, "poll_start", f"poll:{poll.id}", broadcast_id=b.id)
    await _edit_or_send(
        callback,
        f"📋 Опрос #{poll.id} запущен: {b.stats.get('total', 0)} получателей. Сводка обновляется по кнопке.",
        _kb([_btn("📋 Сводка", "a:poll:summary")], [_btn("« Назад", "m:comms")]),
    )


@router.callback_query(F.data == "a:poll:close", RoleFilter(Role.ADMIN))
async def on_poll_close(callback: CallbackQuery, session: AsyncSession) -> None:
    poll = await poll_service.latest_attendance_poll(session)
    if poll is not None:
        poll.is_active = False
        await log_admin_action(session, callback.from_user.id, "poll_close", f"poll:{poll.id}")
    await session.flush()
    await on_comms(callback, session)


@router.callback_query(F.data == "m:content", RoleFilter(Role.ADMIN))
async def on_content(callback: CallbackQuery, state: FSMContext) -> None:
    from app.bot.handlers.admin_content import cmd_content

    await cmd_content(callback.message, state)
    await callback.answer()


# ---------------------------------------------------------------- ⚙️ Команда бота


@router.callback_query(F.data == "m:team", RoleFilter(Role.SUPERADMIN))
async def on_team(callback: CallbackQuery, state: FSMContext, session: AsyncSession, settings: Settings) -> None:
    await state.clear()
    admins = await list_admins(session)
    lines = ["⚙️ <b>Команда бота</b>", "Суперадмины (из настроек): " + (", ".join(f"<code>{u}</code>" for u in sorted(settings.superadmin_ids)) or "—")]
    rows = []
    for a in admins:
        who = f"@{a.username}" if a.username else str(a.telegram_user_id)
        lines.append(f"• {html.escape(who)} — {ROLE_TITLES[a.role]}")
        rows.append([_btn(f"✖️ Убрать {who}", f"a:adm:del:{a.telegram_user_id}")])
    if not admins:
        lines.append("Админов и волонтёров пока нет.")
    rows.append([_btn("➕ Добавить админа или волонтёра", "a:adm:add")])
    rows.append([_btn("🧾 Журнал действий", "a:audit"), _btn("🚨 Подозрительное", "a:susp")])
    rows.append(BACK)
    await _edit_or_send(callback, "\n".join(lines), _kb(*rows))


@router.callback_query(F.data == "a:adm:add", RoleFilter(Role.SUPERADMIN))
async def on_admin_add(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminUI.admin_ref)
    await callback.message.answer(
        "Пришли @username или числовой Telegram ID человека. Username сработает, если человек уже писал боту "
        "(пусть нажмёт /start). ID можно узнать у @userinfobot.",
        reply_markup=_kb([_btn("Отмена", "m:team")]),
    )
    await callback.answer()


@router.message(AdminUI.admin_ref, F.text, ~F.text.startswith("/"), RoleFilter(Role.SUPERADMIN))
async def on_admin_ref(message: Message, state: FSMContext, session: AsyncSession, redis: Redis, settings: Settings) -> None:
    user = await resolve_user(session, redis, message.text)
    if user is None:
        await message.answer("Не знаю такого username. Пусть человек нажмёт /start у бота, или пришли числовой ID.")
        return
    if user.id in settings.superadmin_ids:
        await message.answer("Это суперадмин из настроек — его роль не меняется.")
        await state.clear()
        return
    await state.set_state(None)
    await state.update_data(admin_candidate={"id": user.id, "username": user.username})
    who = f"@{user.username}" if user.username else str(user.id)
    await message.answer(
        f"Какая роль для {html.escape(who)}?",
        reply_markup=_kb(
            [_btn("Админ", "a:adm:role:admin"), _btn("Волонтёр", "a:adm:role:volunteer")],
            [_btn("Отмена", "m:team")],
        ),
    )


@router.callback_query(F.data.startswith("a:adm:role:"), RoleFilter(Role.SUPERADMIN))
async def on_admin_role(callback: CallbackQuery, state: FSMContext, session: AsyncSession, bot: Bot, settings: Settings) -> None:
    from app.services.admins import ResolvedUser

    candidate = (await state.get_data()).get("admin_candidate")
    if not candidate:
        await callback.answer("Начни заново: «➕ Добавить»", show_alert=True)
        return
    role = ASSIGNABLE_ROLES[callback.data.split(":")[3]]
    user = ResolvedUser(int(candidate["id"]), candidate.get("username"))
    await set_admin(session, user, role, callback.from_user.id)
    await log_admin_action(session, callback.from_user.id, "add_admin", str(user.id), role=role.value)
    await session.commit()
    await apply_admin_commands(bot, user.id, role)
    try:
        await bot.send_message(user.id, f"🛠 Тебе выдана роль «{ROLE_TITLES[role]}» в боте Business Cup. Нажми «{ADMIN_BUTTON}».",
                               reply_markup=admin_reply_keyboard())
    except Exception:
        await callback.message.answer("ℹ️ Сообщить человеку не удалось — он ещё не открывал бота. Роль уже действует.")
    await on_team(callback, state, session, settings)


@router.callback_query(F.data.startswith("a:adm:del:"), RoleFilter(Role.SUPERADMIN))
async def on_admin_delete(callback: CallbackQuery, state: FSMContext, session: AsyncSession, bot: Bot, settings: Settings) -> None:
    uid = int(callback.data.split(":")[3])
    removed = await remove_admin(session, uid)
    if removed is not None:
        await log_admin_action(session, callback.from_user.id, "remove_admin", str(uid), role=removed.role.value)
        await session.commit()
        await apply_admin_commands(bot, uid, None)
    await on_team(callback, state, session, settings)


@router.callback_query(F.data == "a:audit", RoleFilter(Role.SUPERADMIN))
async def on_audit(callback: CallbackQuery, session: AsyncSession, settings: Settings) -> None:
    from app.bot.handlers.admin_panel import cmd_audit

    await cmd_audit(callback.message, session, settings)
    await callback.answer()

