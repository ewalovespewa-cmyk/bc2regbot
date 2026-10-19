"""«Задать вопрос»: участник пишет — вопрос уходит в чат организаторов с кнопкой «Ответить».

Ответить можно двумя способами: нажать «✍️ Ответить» и ответить (reply) на сообщение бота
с подсказкой либо сразу ответить reply-ем на сам вопрос. Ответ бот пересылает участнику.
"""
import html

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramForbiddenError
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, ForceReply, InlineKeyboardButton, InlineKeyboardMarkup, Message
from redis.asyncio import Redis
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.filters import InAdminChat, Registered, RoleFilter
from app.bot.keyboards import MENU_ITEMS, answer_question_keyboard, cancel_keyboard, main_menu, menu_text
from app.bot.states import QuestionStates
from app.config import Settings
from app.db.models import MediaType, Participant, Question, Role
from app.i18n import SUPPORTED_LANGS, t
from app.services import ratelimit
from app.services.audit import log_admin_action
from app.services.importer import team_display_name
from app.services.questions import create_question, find_by_admin_message, mark_answered

router = Router(name="questions")
router.message.filter(F.chat.type == "private", Registered())

admin_router = Router(name="questions_admin")
admin_router.message.filter(InAdminChat())
admin_router.callback_query.filter(InAdminChat())

QUESTION_LIMIT = (3, 600)  # не больше 3 вопросов за 10 минут
MAX_LEN = 3000
PROMPT_TTL = 2 * 86400
MENU_LABELS = {t(f"menu.{key}", lang) for key in MENU_ITEMS for lang in SUPPORTED_LANGS}


@router.message(menu_text("ask"))
async def ask_question(message: Message, state: FSMContext, redis: Redis, settings: Settings, participant: Participant) -> None:
    lang = participant.lang
    if not settings.admin_chat_id:
        await message.answer(t("question.no_admin_chat", lang, contact=settings.organizer_contact))
        return
    key = f"question:{participant.id}"
    if int(await redis.get(f"rl:{key}") or 0) >= QUESTION_LIMIT[0]:
        minutes = max(1, await ratelimit.retry_after(redis, key) // 60)
        await message.answer(t("question.rate_limited", lang, minutes=minutes), reply_markup=main_menu(lang))
        return
    await state.set_state(QuestionStates.waiting_question)
    await message.answer(t("question.prompt", lang), reply_markup=cancel_keyboard(lang))


@router.message(QuestionStates.waiting_question, F.text.in_({t("question.cancel", lang) for lang in SUPPORTED_LANGS}))
async def cancel_question(message: Message, state: FSMContext, participant: Participant) -> None:
    await state.clear()
    await message.answer(t("question.cancelled", participant.lang), reply_markup=main_menu(participant.lang))


@router.message(QuestionStates.waiting_question)
async def receive_question(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    bot: Bot,
    redis: Redis,
    settings: Settings,
    participant: Participant,
) -> None:
    lang = participant.lang
    if message.text in MENU_LABELS:
        # Нажал кнопку меню вместо вопроса — считаем это отменой, а не вопросом организаторам.
        await state.clear()
        await message.answer(t("question.cancelled", lang), reply_markup=main_menu(lang))
        return
    if message.photo:
        text, media_type, file_id = message.caption, MediaType.PHOTO, message.photo[-1].file_id
    elif message.text:
        text, media_type, file_id = message.text, None, None
    else:
        await message.answer(t("question.unsupported", lang))
        return
    if text and len(text) > MAX_LEN:
        await message.answer(t("question.too_long", lang, limit=MAX_LEN))
        return
    if not await ratelimit.hit(redis, f"question:{participant.id}", *QUESTION_LIMIT):
        await state.clear()
        minutes = max(1, await ratelimit.retry_after(redis, f"question:{participant.id}") // 60)
        await message.answer(t("question.rate_limited", lang, minutes=minutes), reply_markup=main_menu(lang))
        return

    question = await create_question(session, participant, text=text, media_type=media_type, media_file_id=file_id)
    header = _question_header(question, participant, message.from_user.username)
    body = f"{header}\n\n{html.escape(text)}" if text else header
    markup = answer_question_keyboard(question.id)
    if file_id:
        sent = await bot.send_photo(settings.admin_chat_id, file_id, caption=body[:1024], reply_markup=markup)
    else:
        sent = await bot.send_message(settings.admin_chat_id, body, reply_markup=markup)
    question.admin_chat_message_id = sent.message_id

    await state.clear()
    await message.answer(t("question.sent", lang), reply_markup=main_menu(lang))


def _question_header(question: Question, participant: Participant, username: str | None) -> str:
    who = f"@{html.escape(username)}" if username else f"id {participant.telegram_user_id}"
    team = html.escape(team_display_name(participant.team))
    return (
        f"❓ <b>Вопрос #{question.id}</b>\n"
        f"От: {html.escape(participant.full_name)} ({who})\n"
        f"Команда: {team}"
    )


# ---------------------------------------------------------------- сторона организаторов


@admin_router.callback_query(F.data.startswith("q:answer:"), RoleFilter(Role.ADMIN))
async def on_answer_button(callback: CallbackQuery, bot: Bot, redis: Redis, settings: Settings) -> None:
    question_id = int(callback.data.split(":")[2])
    prompt = await bot.send_message(
        callback.message.chat.id,
        f"✍️ Ответ на вопрос #{question_id} — ответь (reply) на это сообщение.",
        reply_markup=ForceReply(selective=True, input_field_placeholder="Ответ участнику…"),
        reply_to_message_id=callback.message.message_id,
    )
    await redis.set(f"qprompt:{prompt.chat.id}:{prompt.message_id}", question_id, ex=PROMPT_TTL)
    await callback.answer()


@admin_router.callback_query(F.data.startswith("q:"))
async def on_answer_button_denied(callback: CallbackQuery) -> None:
    await callback.answer("Отвечать на вопросы могут только админы.", show_alert=True)


@admin_router.message(F.reply_to_message, F.text | F.photo, RoleFilter(Role.ADMIN))
async def on_admin_reply(
    message: Message, session: AsyncSession, bot: Bot, redis: Redis, settings: Settings
) -> None:
    replied = message.reply_to_message
    question_id = await redis.get(f"qprompt:{message.chat.id}:{replied.message_id}")
    question = await session.get(Question, int(question_id)) if question_id else await find_by_admin_message(session, replied.message_id)
    if question is None:
        return  # обычный разговор в чате, не ответ на вопрос

    participant = await session.get(Participant, question.participant_id)
    if participant is None or participant.telegram_user_id is None:
        await message.reply("⚠️ Участник больше не привязан к боту — ответ не доставить.")
        return

    answer_text = message.text or message.caption or ""
    body = t("question.answer", participant.lang, answer=html.escape(answer_text))
    try:
        if message.photo:
            await bot.send_photo(participant.telegram_user_id, message.photo[-1].file_id, caption=body[:1024])
        else:
            await bot.send_message(participant.telegram_user_id, body)
    except TelegramForbiddenError:
        await session.execute(update(Participant).where(Participant.id == participant.id).values(is_blocked_bot=True))
        await message.reply("⚠️ Участник заблокировал бота — ответ не доставлен.")
        return

    await mark_answered(session, question.id, answer_text, message.from_user.id)
    await log_admin_action(session, message.from_user.id, "answer_question", f"question:{question.id}")
    await message.reply(f"✅ Ответ на вопрос #{question.id} отправлен участнику.")
    if question.admin_chat_message_id:
        who = f"@{message.from_user.username}" if message.from_user.username else message.from_user.full_name
        try:
            await bot.edit_message_reply_markup(
                chat_id=settings.admin_chat_id,
                message_id=question.admin_chat_message_id,
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text=f"✅ Ответил {who} · ответить ещё", callback_data=f"q:answer:{question.id}")]
                    ]
                ),
            )
        except Exception:
            pass  # сообщение могли удалить — не критично
