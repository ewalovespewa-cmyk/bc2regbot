"""Конструктор рассылок: /broadcast (создать), /broadcasts (список, отмена).

Шаги: сообщение (текст с форматированием, можно фото/видео/документ) → кнопки-ссылки →
сегмент → предпросмотр с числом получателей → отправить сейчас / запланировать /
сохранить как шаблон. Черновик хранится в БД с первого шага — рестарт ничего не теряет.
"""
import html
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.filters import RoleFilter
from app.config import Settings
from app.db.models import Broadcast, BroadcastStatus, EventDay, MediaType, Role, Team
from app.services import segments
from app.services.audit import log_admin_action
from app.services.broadcast import (
    CAPTION_LIMIT,
    TEXT_LIMIT,
    cancel_broadcast,
    parse_buttons,
    send_content,
    start_broadcast,
)
from app.services.search import _like

router = Router(name="admin_broadcast")
router.message.filter(F.chat.type == "private")
router.callback_query.filter(F.message.chat.type == "private")

STATUS_TITLES = {
    BroadcastStatus.DRAFT: "черновик",
    BroadcastStatus.SCHEDULED: "запланирована",
    BroadcastStatus.RUNNING: "идёт",
    BroadcastStatus.DONE: "завершена",
    BroadcastStatus.CANCELLED: "отменена",
}


class BroadcastStates(StatesGroup):
    content = State()
    buttons = State()
    segment = State()
    team_query = State()
    schedule_time = State()


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def _kb(*rows: list[InlineKeyboardButton]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=list(rows))


async def _draft(session: AsyncSession, state: FSMContext) -> Broadcast | None:
    data = await state.get_data()
    bid = data.get("broadcast_id")
    return await session.get(Broadcast, bid) if bid else None


# ---------------------------------------------------------------- старт


@router.message(Command("broadcast"), RoleFilter(Role.ADMIN))
async def cmd_broadcast(message: Message, state: FSMContext, session: AsyncSession) -> None:
    await state.clear()
    templates = (
        await session.scalars(select(Broadcast).where(Broadcast.is_template).order_by(Broadcast.id.desc()).limit(8))
    ).all()
    rows = [[_btn("➕ Новая рассылка", "bc:new")]]
    rows += [[_btn(f"📄 Шаблон: {t.title[:40]}", f"bc:tpl:{t.id}")] for t in templates]
    await message.answer("📣 <b>Рассылка</b>\nСписок и отмена: /broadcasts", reply_markup=_kb(*rows))


@router.callback_query(F.data == "bc:new", RoleFilter(Role.ADMIN))
async def on_new(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(BroadcastStates.content)
    await callback.message.answer(
        "Пришли сообщение рассылки одним сообщением: текст с форматированием или фото/видео/документ с подписью.\n"
        "<code>{name}</code> в тексте заменится на имя участника.\nОтмена: /cancel"
    )
    await callback.answer()


@router.callback_query(F.data.startswith("bc:tpl:"), RoleFilter(Role.ADMIN))
async def on_template(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    tpl = await session.get(Broadcast, int(callback.data.split(":")[2]))
    if tpl is None:
        await callback.answer("Шаблон не найден", show_alert=True)
        return
    b = Broadcast(
        title=tpl.title,
        text_html=tpl.text_html,
        media_type=tpl.media_type,
        media_file_id=tpl.media_file_id,
        buttons=tpl.buttons,
        created_by=callback.from_user.id,
    )
    session.add(b)
    await session.flush()
    await state.set_data({"broadcast_id": b.id})
    await callback.answer()
    await _ask_segment(callback.message, state, session)


@router.message(Command("cancel"), RoleFilter(Role.ADMIN))
async def cmd_cancel(message: Message, state: FSMContext, session: AsyncSession) -> None:
    b = await _draft(session, state)
    if b is not None and b.status == BroadcastStatus.DRAFT and not b.is_template:
        await session.delete(b)
    await state.clear()
    await message.answer("Отменено.")


# ---------------------------------------------------------------- шаг 1: контент


@router.message(BroadcastStates.content, ~F.text.startswith("/"), RoleFilter(Role.ADMIN))
async def on_content(message: Message, state: FSMContext, session: AsyncSession) -> None:
    media_type, file_id = None, None
    if message.photo:
        media_type, file_id = MediaType.PHOTO, message.photo[-1].file_id
    elif message.video:
        media_type, file_id = MediaType.VIDEO, message.video.file_id
    elif message.document:
        media_type, file_id = MediaType.DOCUMENT, message.document.file_id
    elif not message.text:
        await message.answer("Нужен текст, фото, видео или документ.")
        return
    text = message.html_text if (message.text or message.caption) else ""
    limit = CAPTION_LIMIT if media_type else TEXT_LIMIT
    if len(text) > limit:
        await message.answer(f"Слишком длинно: {len(text)} символов, максимум {limit}{' для подписи к медиа' if media_type else ''}.")
        return

    plain = (message.text or message.caption or "медиа").strip().splitlines()[0]
    b = Broadcast(
        title=plain[:60],
        text_html=text,
        media_type=media_type,
        media_file_id=file_id,
        created_by=message.from_user.id,
    )
    session.add(b)
    await session.flush()
    await state.set_data({"broadcast_id": b.id})
    await state.set_state(BroadcastStates.buttons)
    await message.answer(
        "Добавить кнопки-ссылки? Пришли строки вида\n<code>Регистрация | https://example.com</code>\n(по одной кнопке на строку)",
        reply_markup=_kb([_btn("Без кнопок", "bc:nobuttons")]),
    )


# ---------------------------------------------------------------- шаг 2: кнопки


@router.message(BroadcastStates.buttons, F.text, ~F.text.startswith("/"), RoleFilter(Role.ADMIN))
async def on_buttons(message: Message, state: FSMContext, session: AsyncSession) -> None:
    try:
        buttons = parse_buttons(message.text)
    except ValueError as exc:
        await message.answer(f"❌ {html.escape(str(exc))}. Попробуй ещё раз или нажми «Без кнопок».")
        return
    b = await _draft(session, state)
    b.buttons = buttons
    await _ask_segment(message, state, session)


@router.callback_query(F.data == "bc:nobuttons", BroadcastStates.buttons, RoleFilter(Role.ADMIN))
async def on_no_buttons(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    await callback.answer()
    await _ask_segment(callback.message, state, session)


# ---------------------------------------------------------------- шаг 3: сегмент


async def _ask_segment(message: Message, state: FSMContext, session: AsyncSession) -> None:
    await state.set_state(BroadcastStates.segment)
    days = (await session.scalars(select(EventDay).order_by(EventDay.date))).all()
    rows = [
        [_btn("👥 Все привязанные", "bc:seg:all")],
        [_btn("✅ Придут", "bc:seg:att:yes"), _btn("❌ Не смогут", "bc:seg:att:no")],
        [_btn("🤔 Не знают", "bc:seg:att:maybe"), _btn("🙈 Не ответили", "bc:seg:att:none")],
    ]
    for d in days:
        rows.append([_btn(f"Отмечены · {d.label}", f"bc:seg:ci:{d.id}"), _btn(f"Не отмечены · {d.label}", f"bc:seg:nci:{d.id}")])
        rows.append([_btn(f"💌 «Ждём тебя»: придут, но не отмечены · {d.label}", f"bc:seg:wait:{d.id}")])
    rows.append([_btn("🏷 Конкретная команда", "bc:seg:team"), _btn("🧍 Соло", "bc:seg:solo")])
    rows.append([_btn("🧪 Тест: только мне", "bc:seg:self")])
    await message.answer("Кому отправляем?", reply_markup=_kb(*rows))


def _segment_from_callback(data: str) -> dict | None:
    parts = data.split(":")[2:]
    match parts:
        case ["all"]:
            return {"type": segments.ALL}
        case ["att", answer]:
            return {"type": segments.ATTENDANCE, "answer": answer}
        case ["ci", day]:
            return {"type": segments.CHECKED_IN, "day_id": int(day)}
        case ["nci", day]:
            return {"type": segments.NOT_CHECKED_IN, "day_id": int(day)}
        case ["wait", day]:
            return {"type": segments.CONFIRMED_NOT_CHECKED_IN, "day_id": int(day)}
        case ["teamid", team_id]:
            return {"type": segments.TEAM, "team_id": int(team_id)}
        case ["solo"]:
            return {"type": segments.SOLO}
        case ["self"]:
            return {"type": segments.SELF}
    return None


@router.callback_query(F.data == "bc:seg:team", RoleFilter(Role.ADMIN))
async def on_team_segment(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(BroadcastStates.team_query)
    await callback.message.answer("Как называется команда? (можно часть названия)")
    await callback.answer()


@router.message(BroadcastStates.team_query, F.text, ~F.text.startswith("/"), RoleFilter(Role.ADMIN))
async def on_team_query(message: Message, state: FSMContext, session: AsyncSession, settings: Settings, bot: Bot) -> None:
    teams = (
        await session.scalars(
            select(Team).where(Team.is_solo.is_(False), Team.name.ilike(_like(message.text.strip()), escape="\\")).limit(10)
        )
    ).all()
    if not teams:
        await message.answer("Не нашёл такую команду, попробуй ещё раз.")
        return
    await state.set_state(BroadcastStates.segment)
    await message.answer(
        "Выбери команду:", reply_markup=_kb(*[[_btn(t.name[:60], f"bc:seg:teamid:{t.id}")] for t in teams])
    )


@router.callback_query(F.data.startswith("bc:seg:"), BroadcastStates.segment, RoleFilter(Role.ADMIN))
async def on_segment(callback: CallbackQuery, state: FSMContext, session: AsyncSession, settings: Settings, bot: Bot) -> None:
    segment = _segment_from_callback(callback.data)
    b = await _draft(session, state)
    if segment is None or b is None:
        await callback.answer("Черновик потерян, начни заново: /broadcast", show_alert=True)
        return
    b.segment = segment
    await session.flush()
    await callback.answer()
    await _preview(callback.message, b, session, settings, bot, callback.from_user.id)


# ---------------------------------------------------------------- шаг 4: предпросмотр и действия


async def _preview(message: Message, b: Broadcast, session: AsyncSession, settings: Settings, bot: Bot, admin_id: int) -> None:
    await message.answer("👀 <b>Так увидят участники:</b>")
    await send_content(bot, admin_id, b, name="Аружан")
    count = await segments.count_recipients(session, b.segment)
    description = await segments.describe(session, b.segment)
    test = ""
    if settings.test_mode and b.segment.get("type") != segments.SELF:
        test = "\n🧪 <b>TEST_MODE включён</b>: на самом деле получат только админы."
    await message.answer(
        f"Сегмент: {html.escape(description)}\nПолучателей: <b>{count}</b>{test}",
        reply_markup=_kb(
            [_btn("🚀 Отправить сейчас", "bc:send")],
            [_btn("🕒 Запланировать", "bc:schedule"), _btn("💾 В шаблоны", "bc:tplsave")],
            [_btn("🔁 Другой сегмент", "bc:reseg"), _btn("✖️ Отмена", "bc:drop")],
        ),
    )


@router.callback_query(F.data == "bc:reseg", RoleFilter(Role.ADMIN))
async def on_reseg(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    await callback.answer()
    await _ask_segment(callback.message, state, session)


@router.callback_query(F.data == "bc:send", RoleFilter(Role.ADMIN))
async def on_send(callback: CallbackQuery, state: FSMContext, session: AsyncSession, settings: Settings) -> None:
    b = await _draft(session, state)
    if b is None or b.status != BroadcastStatus.DRAFT:
        await callback.answer("Эта рассылка уже запущена или отменена", show_alert=True)
        return
    total = await start_broadcast(session, b, settings)
    await log_admin_action(session, callback.from_user.id, "broadcast_start", f"broadcast:{b.id}", total=total, segment=b.segment)
    await state.clear()
    await callback.message.edit_text(
        f"🚀 Рассылка #{b.id} запущена: {total} получателей. Отчёт придёт по завершении. Отменить: /broadcasts"
    )
    await callback.answer()


@router.callback_query(F.data == "bc:schedule", RoleFilter(Role.ADMIN))
async def on_schedule(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(BroadcastStates.schedule_time)
    await callback.message.answer("Когда отправить? Время Астаны, формат <code>ДД.ММ ЧЧ:ММ</code>, например <code>13.11 10:00</code>")
    await callback.answer()


def parse_local_datetime(text: str, timezone: str, now: datetime | None = None) -> datetime:
    tz = ZoneInfo(timezone)
    now = (now or datetime.now(UTC)).astimezone(tz)
    parsed = datetime.strptime(text.strip(), "%d.%m %H:%M").replace(year=now.year, tzinfo=tz)
    if parsed < now and (now - parsed).days > 180:  # «05.01» в декабре — это следующий год
        parsed = parsed.replace(year=now.year + 1)
    return parsed


@router.message(BroadcastStates.schedule_time, F.text, ~F.text.startswith("/"), RoleFilter(Role.ADMIN))
async def on_schedule_time(message: Message, state: FSMContext, session: AsyncSession, settings: Settings) -> None:
    try:
        when = parse_local_datetime(message.text, settings.timezone)
    except ValueError:
        await message.answer("Не понял время. Формат: <code>ДД.ММ ЧЧ:ММ</code>")
        return
    if when <= datetime.now(UTC):
        await message.answer("Это время уже прошло. Укажи будущее.")
        return
    b = await _draft(session, state)
    if b is None or b.status != BroadcastStatus.DRAFT:
        await message.answer("Черновик потерян, начни заново: /broadcast")
        await state.clear()
        return
    b.status = BroadcastStatus.SCHEDULED
    b.scheduled_at = when
    await log_admin_action(session, message.from_user.id, "broadcast_schedule", f"broadcast:{b.id}", at=when.isoformat())
    await state.clear()
    await message.answer(f"🕒 Рассылка #{b.id} запланирована на {when:%d.%m %H:%M} (Астана). Отменить: /broadcasts")


@router.callback_query(F.data == "bc:tplsave", RoleFilter(Role.ADMIN))
async def on_template_save(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    b = await _draft(session, state)
    if b is None:
        await callback.answer("Черновик потерян", show_alert=True)
        return
    session.add(
        Broadcast(
            title=b.title,
            text_html=b.text_html,
            media_type=b.media_type,
            media_file_id=b.media_file_id,
            buttons=b.buttons,
            is_template=True,
            created_by=callback.from_user.id,
        )
    )
    await callback.answer("💾 Сохранено в шаблоны. Рассылку можно отправить или запланировать.", show_alert=True)


@router.callback_query(F.data == "bc:drop", RoleFilter(Role.ADMIN))
async def on_drop(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    b = await _draft(session, state)
    if b is not None and b.status == BroadcastStatus.DRAFT:
        await session.delete(b)
    await state.clear()
    await callback.message.edit_text("✖️ Черновик удалён.")
    await callback.answer()


# ---------------------------------------------------------------- список и отмена


@router.message(Command("broadcasts"), RoleFilter(Role.ADMIN))
async def cmd_broadcasts(message: Message, session: AsyncSession, settings: Settings) -> None:
    tz = ZoneInfo(settings.timezone)
    items = (
        await session.scalars(
            select(Broadcast).where(Broadcast.is_template.is_(False), Broadcast.status != BroadcastStatus.DRAFT)
            .order_by(Broadcast.id.desc()).limit(10)
        )
    ).all()
    if not items:
        await message.answer("Рассылок ещё не было. Создать: /broadcast")
        return
    lines, rows = ["📣 <b>Последние рассылки</b>"], []
    for b in items:
        s = b.stats or {}
        when = b.scheduled_at if b.status == BroadcastStatus.SCHEDULED else b.started_at
        when_s = f" · {when.astimezone(tz):%d.%m %H:%M}" if when else ""
        progress = f" · ✅{s.get('sent', '…')} 🚫{s.get('blocked', 0)} ❌{s.get('failed', 0)} из {s.get('total', '?')}" if s else ""
        lines.append(f"#{b.id} {html.escape(b.title[:40])} — {STATUS_TITLES[b.status]}{when_s}{progress}")
        if b.status in (BroadcastStatus.SCHEDULED, BroadcastStatus.RUNNING):
            rows.append([_btn(f"⛔ Отменить #{b.id}", f"bc:stop:{b.id}")])
    await message.answer("\n".join(lines), reply_markup=_kb(*rows) if rows else None)


@router.callback_query(F.data.startswith("bc:stop:"), RoleFilter(Role.ADMIN))
async def on_stop(callback: CallbackQuery, session: AsyncSession) -> None:
    b = await session.get(Broadcast, int(callback.data.split(":")[2]), with_for_update=True)
    if b is None or not await cancel_broadcast(session, b):
        await callback.answer("Уже завершена или отменена", show_alert=True)
        return
    await log_admin_action(session, callback.from_user.id, "broadcast_cancel", f"broadcast:{b.id}")
    s = b.stats or {}
    await callback.message.answer(f"⛔ Рассылка #{b.id} отменена. Успели отправить: {s.get('sent', 0)}.")
    await callback.answer()
