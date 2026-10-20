"""Админка: /admin, роли (/addadmin, /removeadmin, /admins), поиск участника (/find), журнал (/audit)."""
import html

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.admin_commands import apply_admin_commands
from app.bot.filters import RoleFilter
from app.config import Settings
from app.db.models import AdminAction, Participant, Role
from app.services.admins import ASSIGNABLE_ROLES, list_admins, remove_admin, resolve_user, set_admin
from app.services.audit import log_admin_action
from app.services.cards import participant_card, short_line
from app.services.linking import unlink
from app.services.roles import has_role
from app.services.search import search_participants

router = Router(name="admin_panel")
router.message.filter(F.chat.type == "private")
router.callback_query.filter(F.message.chat.type == "private")

ROLE_TITLES = {Role.SUPERADMIN: "суперадмин", Role.ADMIN: "админ", Role.VOLUNTEER: "волонтёр"}


class FindStates(StatesGroup):
    waiting_query = State()


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def admin_menu_keyboard(role: Role, settings: Settings | None = None) -> InlineKeyboardMarkup:
    rows = [[_btn("🔎 Найти участника", "adm:find")]]
    if settings is not None and has_role(role, Role.ADMIN):
        from app.bot.handlers.checkin import screen_links_keyboard

        if links := screen_links_keyboard(settings):
            rows.append(links.inline_keyboard[0])
    if has_role(role, Role.ADMIN):
        rows.append([_btn("📊 Статистика", "adm:stats"), _btn("✅ Чекин: дни и экран", "adm:days")])
    if has_role(role, Role.ADMIN):
        rows.append([_btn("📤 Выгрузка CSV", "adm:export"), _btn("🔄 В Google Sheet", "adm:sync")])
        rows.append([_btn("📣 Рассылка", "adm:broadcast"), _btn("📋 Опрос явки", "adm:poll")])
        rows.append([_btn("📝 Контент", "adm:content"), _btn("📞 Обзвон", "adm:calls")])
    if has_role(role, Role.SUPERADMIN):
        rows.append([_btn("📥 Импорт", "adm:import"), _btn("👥 Админы", "adm:admins")])
        rows.append([_btn("🧾 Журнал действий", "adm:audit"), _btn("🚨 Подозрительное", "adm:suspicious")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ---------------------------------------------------------------- /admin


@router.message(Command("admin"), RoleFilter(Role.VOLUNTEER))
async def cmd_admin(message: Message, bot: Bot, role: Role, settings: Settings) -> None:
    await apply_admin_commands(bot, message.from_user.id, role)  # заодно обновим подсказки команд
    await message.answer(
        f"🛠 <b>Админ-меню</b> · роль: {ROLE_TITLES[role]}", reply_markup=admin_menu_keyboard(role, settings)
    )


@router.callback_query(F.data == "adm:find", RoleFilter(Role.VOLUNTEER))
async def on_find_button(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(FindStates.waiting_query)
    await callback.message.answer("Кого ищем? Фамилия, имя, телефон, email, @username или команда.")
    await callback.answer()


@router.callback_query(F.data == "adm:import", RoleFilter(Role.SUPERADMIN))
async def on_import_button(callback: CallbackQuery, settings: Settings) -> None:
    source = "Google Sheet подключён: просто отправь /import." if settings.sheets_enabled else "Google Sheet не подключён."
    await callback.message.answer(
        f"📥 <b>Импорт участников</b>\n{source}\n\n"
        "Из файла: пришли CSV с подписью <code>/import</code> "
        "(или <code>/import all</code>, если в файле только отобранные).\n"
        "Команды отдельным файлом: CSV <code>team_name,email</code> с подписью <code>/import_teams</code>."
    )
    await callback.answer()


@router.callback_query(
    F.data.in_({"adm:export", "adm:sync", "adm:content", "adm:admins", "adm:audit", "adm:broadcast", "adm:poll",
                "adm:calls", "adm:stats", "adm:days", "adm:suspicious"})
)
async def on_shortcut(callback: CallbackQuery) -> None:
    """Кнопки-ярлыки: подсказываем команду, сами команды проверяют роль."""
    command = callback.data.split(":")[1]
    await callback.message.answer(f"Отправь /{command}")
    await callback.answer()


# ---------------------------------------------------------------- роли


@router.message(Command("addadmin"), RoleFilter(Role.SUPERADMIN))
async def cmd_addadmin(
    message: Message, command: CommandObject, session: AsyncSession, redis: Redis, bot: Bot, settings: Settings
) -> None:
    parts = (command.args or "").split()
    if len(parts) != 2 or parts[1].lower() not in ASSIGNABLE_ROLES:
        await message.answer(
            "Формат: <code>/addadmin &lt;id|@username&gt; &lt;admin|volunteer&gt;</code>\n"
            "Суперадмины задаются только переменной SUPERADMIN_IDS."
        )
        return
    user = await resolve_user(session, redis, parts[0])
    if user is None:
        await message.answer(
            "Не знаю такого username. Пусть человек сначала напишет боту /start, "
            "или укажи числовой Telegram ID (узнать: @userinfobot)."
        )
        return
    if user.id in settings.superadmin_ids:
        await message.answer("Это суперадмин из SUPERADMIN_IDS — его роль не меняется командой.")
        return
    role = ASSIGNABLE_ROLES[parts[1].lower()]
    _, previous = await set_admin(session, user, role, message.from_user.id)
    await log_admin_action(
        session, message.from_user.id, "add_admin", str(user.id), role=role.value, previous=previous and previous.value
    )
    await session.commit()
    await apply_admin_commands(bot, user.id, role)

    who = f"@{html.escape(user.username)}" if user.username else f"<code>{user.id}</code>"
    change = f" (было: {ROLE_TITLES[previous]})" if previous and previous != role else ""
    await message.answer(f"✅ {who} теперь {ROLE_TITLES[role]}{change}.")
    try:
        await bot.send_message(user.id, f"🛠 Тебе выдана роль «{ROLE_TITLES[role]}» в боте Business Cup. Меню: /admin")
    except Exception:
        await message.answer("ℹ️ Сообщить человеку не удалось: он ещё не открывал бота. Роль уже действует.")


@router.message(Command("removeadmin"), RoleFilter(Role.SUPERADMIN))
async def cmd_removeadmin(
    message: Message, command: CommandObject, session: AsyncSession, redis: Redis, bot: Bot, settings: Settings
) -> None:
    ref = (command.args or "").strip()
    user = await resolve_user(session, redis, ref) if ref else None
    if user is None:
        await message.answer("Формат: <code>/removeadmin &lt;id|@username&gt;</code>")
        return
    if user.id in settings.superadmin_ids:
        await message.answer("Суперадмина можно убрать только из переменной SUPERADMIN_IDS.")
        return
    removed = await remove_admin(session, user.id)
    if removed is None:
        await message.answer("Такого админа или волонтёра нет.")
        return
    await log_admin_action(session, message.from_user.id, "remove_admin", str(user.id), role=removed.role.value)
    await session.commit()
    await apply_admin_commands(bot, user.id, None)
    await message.answer(f"✅ Роль «{ROLE_TITLES[removed.role]}» снята.")


@router.message(Command("admins"), RoleFilter(Role.SUPERADMIN))
async def cmd_admins(message: Message, session: AsyncSession, settings: Settings) -> None:
    lines = ["👥 <b>Команда бота</b>", "", "Суперадмины (SUPERADMIN_IDS):"]
    lines += [f"• <code>{uid}</code>" for uid in sorted(settings.superadmin_ids)] or ["• —"]
    admins = await list_admins(session)
    for role in (Role.ADMIN, Role.VOLUNTEER):
        group = [a for a in admins if a.role == role]
        lines.append("\nАдмины:" if role == Role.ADMIN else "\nВолонтёры:")
        lines += [
            f"• {('@' + html.escape(a.username)) if a.username else ''} <code>{a.telegram_user_id}</code>" for a in group
        ] or ["• —"]
    await message.answer("\n".join(lines))


@router.message(Command("audit"), RoleFilter(Role.SUPERADMIN))
async def cmd_audit(message: Message, session: AsyncSession, settings: Settings) -> None:
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(settings.timezone)
    actions = (await session.scalars(select(AdminAction).order_by(AdminAction.id.desc()).limit(25))).all()
    if not actions:
        await message.answer("Журнал пуст.")
        return
    lines = ["🧾 <b>Последние действия</b>"]
    for a in actions:
        target = f" → {html.escape(a.target)}" if a.target else ""
        lines.append(f"{a.created_at.astimezone(tz):%d.%m %H:%M} · <code>{a.actor_id}</code> · {html.escape(a.action)}{target}")
    await message.answer("\n".join(lines))


# ---------------------------------------------------------------- поиск и карточка


async def _send_results(message: Message, session: AsyncSession, query: str) -> None:
    found = await search_participants(session, query)
    if not found:
        await message.answer(f"Никого не нашёл по «{html.escape(query)}». Попробуй фамилию, телефон или email.")
        return
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[_btn(short_line(p)[:60], f"p:card:{p.id}")] for p in found]
    )
    more = "\nПоказаны первые 10 — уточни запрос." if len(found) == 10 else ""
    await message.answer(f"Нашёл: {len(found)}{more}", reply_markup=keyboard)


@router.message(Command("find"), RoleFilter(Role.VOLUNTEER))
async def cmd_find(message: Message, command: CommandObject, state: FSMContext, session: AsyncSession) -> None:
    query = (command.args or "").strip()
    if not query:
        await state.set_state(FindStates.waiting_query)
        await message.answer("Кого ищем? Фамилия, имя, телефон, email, @username или команда.")
        return
    await state.clear()
    await _send_results(message, session, query)


@router.message(FindStates.waiting_query, F.text, ~F.text.startswith("/"), RoleFilter(Role.VOLUNTEER))
async def on_find_query(message: Message, state: FSMContext, session: AsyncSession) -> None:
    await state.clear()
    await _send_results(message, session, message.text)


def card_keyboard(p: Participant, role: Role, day=None, checked: bool = False) -> InlineKeyboardMarkup | None:
    from app.bot.handlers.checkin import checkin_card_rows

    rows = checkin_card_rows(p, role, day, checked)
    rows.append([_btn("📞 Результат обзвона", f"pc:ask:{p.id}")])
    if has_role(role, Role.SUPERADMIN) and p.telegram_user_id:
        rows.append([_btn("🔓 Отвязать Telegram", f"p:unlink:{p.id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


@router.callback_query(F.data.startswith("p:card:"), RoleFilter(Role.VOLUNTEER))
async def on_card(callback: CallbackQuery, session: AsyncSession, settings: Settings, role: Role) -> None:
    p = await session.get(Participant, int(callback.data.split(":")[2]))
    if p is None:
        await callback.answer("Участник не найден", show_alert=True)
        return
    from app.bot.handlers.checkin import checked_today
    from app.services.checkin import today_day

    day = await today_day(session, settings)
    await callback.message.answer(
        await participant_card(session, p, role, settings.timezone),
        reply_markup=card_keyboard(p, role, day, await checked_today(session, p.id, day)),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("p:unlink:"), RoleFilter(Role.SUPERADMIN))
async def on_unlink_ask(callback: CallbackQuery, session: AsyncSession) -> None:
    p = await session.get(Participant, int(callback.data.split(":")[2]))
    if p is None or p.telegram_user_id is None:
        await callback.answer("Уже не привязан", show_alert=True)
        return
    await callback.message.answer(
        f"Отвязать Telegram от <b>{html.escape(p.full_name)}</b>? Человек сможет заново войти по своей ссылке "
        "или контакту, а текущий аккаунт потеряет доступ.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[_btn("Да, отвязать", f"p:unlink_ok:{p.id}"), _btn("Отмена", "p:noop")]]
        ),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("p:unlink_ok:"), RoleFilter(Role.SUPERADMIN))
async def on_unlink_confirm(callback: CallbackQuery, session: AsyncSession) -> None:
    p = await session.get(Participant, int(callback.data.split(":")[2]))
    old_id = await unlink(session, p) if p else None
    if old_id is None:
        await callback.answer("Уже не привязан", show_alert=True)
        return
    await log_admin_action(session, callback.from_user.id, "unlink", f"participant:{p.id}", telegram_user_id=old_id)
    await callback.message.edit_text(f"🔓 Telegram <code>{old_id}</code> отвязан от {html.escape(p.full_name)}.")
    await callback.answer()


@router.callback_query(F.data == "p:noop")
async def on_noop(callback: CallbackQuery) -> None:
    await callback.message.edit_text("Отменено.")
    await callback.answer()
