"""Опрос явки: ответы участников, причина отказа, /poll для админов, результат обзвона, выгрузки."""
import html
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.filters import RoleFilter
from app.bot.handlers.admin_broadcast import parse_local_datetime
from app.bot.keyboards import main_menu
from app.config import Settings
from app.db.models import AnswerSource, Participant, Poll, PollOption, Role
from app.i18n import t
from app.services import polls as poll_service
from app.services.audit import log_admin_action
from app.services.sources import rows_to_csv

router = Router(name="polls")
router.message.filter(F.chat.type == "private")


class PollStates(StatesGroup):
    reason = State()


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


# ---------------------------------------------------------------- участник отвечает


@router.callback_query(F.data.startswith("pl:"))
async def on_answer(
    callback: CallbackQuery, state: FSMContext, session: AsyncSession, participant: Participant | None, lang: str
) -> None:
    _, poll_id, option_id = callback.data.split(":")
    poll = await session.get(Poll, int(poll_id))
    option = await session.get(PollOption, int(option_id))
    if participant is None:
        await callback.answer(t("poll.not_participant", lang), show_alert=True)
        return
    if poll is None or option is None or option.poll_id != poll.id:
        await callback.answer()
        return
    if not poll_service.is_open(poll):
        await callback.answer(t("poll.closed", participant.lang), show_alert=True)
        return

    await poll_service.record_answer(session, poll, participant, option)
    label = poll_service.option_label(option, participant.lang)
    await callback.answer(t("poll.saved", participant.lang, answer=label))
    try:
        # Кнопки остаются: ответ можно поменять до дедлайна. Отмечаем выбранный вариант.
        rows = [
            [_btn(("● " if o.id == option.id else "") + poll_service.option_label(o, participant.lang), f"pl:{poll.id}:{o.id}")]
            for o in poll.options
        ]
        await callback.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    except Exception:
        pass
    if option.asks_reason:
        await state.set_state(PollStates.reason)
        await state.update_data(poll_id=poll.id)
        await callback.message.answer(
            t("poll.ask_reason", participant.lang),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[_btn(t("poll.skip", participant.lang), "plr:skip")]]),
        )


@router.message(PollStates.reason, F.text, ~F.text.startswith("/"))
async def on_reason(message: Message, state: FSMContext, session: AsyncSession, participant: Participant | None) -> None:
    data = await state.get_data()
    await state.clear()
    if participant is None:
        return
    await poll_service.set_reason(session, data["poll_id"], participant.id, message.text)
    await message.answer(t("poll.reason_thanks", participant.lang), reply_markup=main_menu(participant.lang))


@router.callback_query(F.data == "plr:skip")
async def on_reason_skip(callback: CallbackQuery, state: FSMContext, participant: Participant | None, lang: str) -> None:
    await state.clear()
    await callback.message.edit_text(t("poll.reason_skipped", participant.lang if participant else lang))
    await callback.answer()


# ---------------------------------------------------------------- админ


@router.message(Command("poll"), RoleFilter(Role.ADMIN))
async def cmd_poll(message: Message, command: CommandObject, session: AsyncSession, settings: Settings) -> None:
    """/poll — сводка. /poll start [ДД.ММ ЧЧ:ММ дедлайн] [часов до напоминания] [сколько напоминаний]."""
    args = (command.args or "").split()
    poll = await poll_service.latest_attendance_poll(session)

    if args and args[0] == "start":
        if poll and poll_service.is_open(poll):
            await message.answer("Опрос явки уже идёт. Сначала закрой его: <code>/poll close</code>")
            return
        deadline, hours, reminders = None, 6, 1
        try:
            if len(args) >= 3:
                deadline = parse_local_datetime(f"{args[1]} {args[2]}", settings.timezone)
            if len(args) >= 4:
                hours = int(args[3])
            if len(args) >= 5:
                reminders = int(args[4])
        except ValueError:
            await message.answer("Формат: <code>/poll start 13.11 20:00 6 1</code> — дедлайн, через сколько часов напомнить, сколько раз.")
            return
        poll = await poll_service.create_attendance_poll(
            session, created_by=message.from_user.id, deadline_at=deadline, reminder_after_hours=hours, max_reminders=reminders
        )
        b = await poll_service.launch_poll(session, poll, settings)
        await log_admin_action(session, message.from_user.id, "poll_start", f"poll:{poll.id}", broadcast_id=b.id)
        tz = ZoneInfo(settings.timezone)
        dl = f"до {deadline.astimezone(tz):%d.%m %H:%M}" if deadline else "без дедлайна"
        await message.answer(
            f"📋 Опрос #{poll.id} запущен ({dl}), рассылка #{b.id}: {b.stats.get('total', 0)} получателей.\n"
            f"Напоминание не ответившим и «не знаю»: через {hours} ч, раз: {reminders}."
        )
        return

    if poll is None:
        await message.answer(
            "Опроса явки ещё не было.\nЗапуск: <code>/poll start</code> или <code>/poll start 13.11 20:00 6 1</code> "
            "(дедлайн, через сколько часов напомнить, сколько раз)."
        )
        return

    if args and args[0] == "close":
        poll.is_active = False
        await log_admin_action(session, message.from_user.id, "poll_close", f"poll:{poll.id}")
        await message.answer(f"Опрос #{poll.id} закрыт: ответы больше не принимаются.")
        return

    await message.answer(
        await poll_service.poll_summary(session, poll),
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [_btn("📞 Список на обзвон (CSV)", "pa:calls"), _btn("👥 Отчёт по командам", "pa:teams")],
            ]
        ),
    )


@router.message(Command("calls"), RoleFilter(Role.ADMIN))
async def cmd_calls(message: Message, session: AsyncSession, settings: Settings) -> None:
    await _send_calls(message, session, settings)


@router.callback_query(F.data == "pa:calls", RoleFilter(Role.ADMIN))
async def on_calls(callback: CallbackQuery, session: AsyncSession, settings: Settings) -> None:
    await _send_calls(callback.message, session, settings)
    await callback.answer()


async def _send_calls(message: Message, session: AsyncSession, settings: Settings) -> None:
    poll = await poll_service.latest_attendance_poll(session)
    header, rows = await poll_service.call_list(session, poll)
    stamp = datetime.now(ZoneInfo(settings.timezone)).strftime("%Y%m%d_%H%M")
    await message.answer_document(
        BufferedInputFile(rows_to_csv(header, rows), filename=f"calls_{stamp}.csv"),
        caption=f"📞 На обзвон: {len(rows)} человек (не открыли бота, не ответили, «не знаю»).\n"
        "Результат звонка вноси в карточке участника: /find → «📞 Результат обзвона».",
    )
    if settings.sheets_enabled:
        from app.services.sources import SheetsClient, SheetsError

        try:
            await SheetsClient(settings).write_table("calls", header, rows)
            await message.answer("То же записано на лист «calls» в Google Sheet.")
        except SheetsError as exc:
            await message.answer(f"⚠️ В Google Sheet записать не вышло: {html.escape(str(exc))}")


@router.callback_query(F.data == "pa:teams", RoleFilter(Role.ADMIN))
async def on_team_report(callback: CallbackQuery, session: AsyncSession) -> None:
    poll = await poll_service.latest_attendance_poll(session)
    if poll is None:
        await callback.answer("Опроса ещё не было", show_alert=True)
        return
    r = await poll_service.team_report(session, poll)

    def block(title: str, items: list[str]) -> str:
        body = "\n".join(f"• {html.escape(i)}" for i in items[:60]) or "—"
        more = f"\n… и ещё {len(items) - 60}" if len(items) > 60 else ""
        return f"<b>{title}</b> ({len(items)})\n{body}{more}"

    text = "\n\n".join(
        [
            block("✅ Все подтвердили", r.all_confirmed),
            block("❌ Кто-то отказался", r.someone_declined),
            block("🙈 Никто не ответил", r.nobody_answered),
            block("⏳ Частично", r.partial),
        ]
    )
    await callback.message.answer(text[:4096])
    await callback.answer()


# ---------------------------------------------------------------- результат обзвона из карточки


@router.callback_query(F.data.startswith("pc:ask:"), RoleFilter(Role.VOLUNTEER))
async def on_call_ask(callback: CallbackQuery, session: AsyncSession) -> None:
    pid = int(callback.data.split(":")[2])
    poll = await poll_service.latest_attendance_poll(session)
    if poll is None:
        await callback.answer("Опроса явки ещё не было", show_alert=True)
        return
    rows = [[_btn(o.label_ru, f"pc:set:{pid}:{o.id}")] for o in poll.options]
    await callback.message.answer("📞 Что ответил участник по телефону?", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await callback.answer()


@router.callback_query(F.data.startswith("pc:set:"), RoleFilter(Role.VOLUNTEER))
async def on_call_set(callback: CallbackQuery, session: AsyncSession) -> None:
    _, _, pid, oid = callback.data.split(":")
    participant = await session.get(Participant, int(pid))
    option = await session.get(PollOption, int(oid))
    poll = await session.get(Poll, option.poll_id) if option else None
    if participant is None or poll is None:
        await callback.answer("Не найдено", show_alert=True)
        return
    await poll_service.record_answer(
        session, poll, participant, option, source=AnswerSource.CALL, answered_by=callback.from_user.id
    )
    await log_admin_action(session, callback.from_user.id, "call_result", f"participant:{participant.id}", option=option.code)
    await callback.message.edit_text(
        f"📞 Записал: {html.escape(participant.full_name)} — {html.escape(option.label_ru)} (по обзвону)."
    )
    await callback.answer()
