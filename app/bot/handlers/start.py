"""Привязка аккаунта: /start <токен> из персональной ссылки или контакт с номером из заявки."""
import html

from aiogram import Bot, F, Router
from aiogram.filters import CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, ReplyKeyboardRemove, User
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.keyboards import contact_keyboard, games_keyboard, main_menu
from app.bot.states import LinkStates
from app.config import Settings
from app.db.models import Participant
from app.i18n import t
from app.bot.checkin_flow import call_organizer_keyboard, finish_pending, handle_code
from app.services import checkin_codes, ratelimit, security
from app.services.importer import team_display_name
from app.services.linking import LinkResult, LinkStatus, TelegramUser, link_by_contact, link_by_token
from app.services.normalize import normalize_phone
from app.services.tokens import is_invite_token

router = Router(name="start")
router.message.filter(F.chat.type == "private")

BAD_TOKEN_LIMIT = (10, 3600)  # попыток с неизвестной ссылкой в час
CONTACT_LIMIT = (5, 3600)  # попыток привязки по контакту в час


def _tg_user(user: User) -> TelegramUser:
    return TelegramUser(id=user.id, username=user.username, language_code=user.language_code)


async def send_welcome(message: Message, participant: Participant, settings: Settings, *, first_time: bool) -> None:
    lang = participant.lang
    name = html.escape(participant.full_name.split()[0] if participant.full_name else "")
    if first_time:
        team = participant.team
        team_line = (
            t("start.welcome_solo", lang)
            if team is None or team.is_solo
            else t("start.welcome_team", lang, team=html.escape(team_display_name(team, lang)))
        )
        await message.answer(t("start.welcome", lang, name=name, team_line=team_line), reply_markup=main_menu(lang))
        if games := games_keyboard(settings, lang):
            await message.answer(t("start.games_hint", lang), reply_markup=games)
    else:
        await message.answer(t("start.welcome_back", lang, name=name), reply_markup=main_menu(lang))


async def ask_contact(message: Message, state: FSMContext, lang: str) -> None:
    await state.set_state(LinkStates.waiting_contact)
    await message.answer(t("start.ask_contact", lang), reply_markup=contact_keyboard(lang))


async def _handle_result(
    message: Message,
    result: LinkResult,
    *,
    via: str,
    lang: str,
    state: FSMContext,
    session: AsyncSession,
    bot: Bot,
    redis: Redis,
    settings: Settings,
) -> None:
    user = message.from_user
    contact = settings.organizer_contact
    report = dict(session=session, bot=bot, redis=redis, settings=settings, telegram_user_id=user.id, username=user.username)

    pending = (await state.get_data()).get("pending_checkin")
    await state.clear()
    if result.ok:
        await send_welcome(message, result.participant, settings, first_time=result.status == LinkStatus.LINKED)
        await finish_pending(message, pending, result.participant, session, settings)
        return

    if result.status == LinkStatus.NOT_FOUND:
        await message.answer(t("start.not_found", lang, contact=contact), reply_markup=ReplyKeyboardRemove())
        if pending:  # человек на входе, а номер не совпал — пусть позовёт волонтёра
            await message.answer(t("checkin.phone_mismatch", lang), reply_markup=call_organizer_keyboard(lang))
    elif result.status == LinkStatus.TOKEN_TAKEN:
        p = result.participant
        await message.answer(t("start.token_taken", lang, contact=contact), reply_markup=ReplyKeyboardRemove())
        await security.report(
            kind=security.TOKEN_OWNED_BY_OTHER,
            participant_id=p.id,
            via=via,
            participant=f"{p.full_name} <{p.email}>",
            owner_telegram_id=p.telegram_user_id,
            **report,
        )
    elif result.status == LinkStatus.ACCOUNT_TAKEN:
        other, p = result.other, result.participant
        await message.answer(
            t("start.account_taken", lang, name=html.escape(other.full_name), contact=contact),
            reply_markup=main_menu(other.lang) if other.is_selected else ReplyKeyboardRemove(),
        )
        await security.report(
            kind=security.ACCOUNT_LINKED_TO_OTHER,
            participant_id=p.id,
            via=via,
            linked_to=f"{other.full_name} <{other.email}>",
            tried=f"{p.full_name} <{p.email}>",
            **report,
        )
    elif result.status == LinkStatus.AMBIGUOUS:
        await message.answer(t("start.ambiguous", lang, contact=contact), reply_markup=ReplyKeyboardRemove())
        await security.report(kind=security.AMBIGUOUS_PHONE, via=via, phone=result.participant.phone, **report)


@router.message(CommandStart(deep_link=True))
async def start_with_payload(
    message: Message,
    command: CommandObject,
    state: FSMContext,
    session: AsyncSession,
    bot: Bot,
    redis: Redis,
    settings: Settings,
    participant: Participant | None,
    lang: str,
) -> None:
    payload = (command.args or "").strip()
    if payload.startswith(checkin_codes.PREFIX):
        await handle_code(
            message, checkin_codes.extract_code(payload), state=state, session=session, bot=bot, redis=redis,
            settings=settings, participant=participant, lang=lang,
        )
        return
    if is_invite_token(payload):
        result = await link_by_token(session, _tg_user(message.from_user), payload)
        if result.status != LinkStatus.NOT_FOUND:
            await _handle_result(
                message, result, via="link", lang=lang, state=state, session=session, bot=bot, redis=redis, settings=settings
            )
            return

    # Неизвестная или битая ссылка: для привязанного это не проблема, остальных ведём через контакт.
    if participant:
        await send_welcome(message, participant, settings, first_time=False)
        return
    limit, window = BAD_TOKEN_LIMIT
    if not await ratelimit.hit(redis, f"badtoken:{message.from_user.id}", limit, window):
        minutes = max(1, await ratelimit.retry_after(redis, f"badtoken:{message.from_user.id}") // 60)
        await message.answer(t("start.too_many_attempts", lang, minutes=minutes, contact=settings.organizer_contact))
        await security.report(
            session,
            bot=bot,
            redis=redis,
            settings=settings,
            kind=security.INVALID_TOKEN_FLOOD,
            telegram_user_id=message.from_user.id,
            username=message.from_user.username,
            last_payload=payload[:64],
        )
        return
    await ask_contact(message, state, lang)


@router.message(CommandStart())
async def start_plain(
    message: Message, state: FSMContext, settings: Settings, participant: Participant | None, lang: str
) -> None:
    if participant:
        await state.clear()
        await send_welcome(message, participant, settings, first_time=False)
        return
    await ask_contact(message, state, lang)


@router.message(F.contact)
async def on_contact(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    bot: Bot,
    redis: Redis,
    settings: Settings,
    participant: Participant | None,
    lang: str,
) -> None:
    if participant:
        await send_welcome(message, participant, settings, first_time=False)
        return
    user = message.from_user
    if message.contact.user_id != user.id:
        await message.answer(t("start.foreign_contact", lang), reply_markup=contact_keyboard(lang))
        await security.report(
            session,
            bot=bot,
            redis=redis,
            settings=settings,
            kind=security.FOREIGN_CONTACT,
            telegram_user_id=user.id,
            username=user.username,
            notify=False,
            contact_user_id=message.contact.user_id,
        )
        return

    limit, window = CONTACT_LIMIT
    if not await ratelimit.hit(redis, f"contact:{user.id}", limit, window):
        minutes = max(1, await ratelimit.retry_after(redis, f"contact:{user.id}") // 60)
        await message.answer(t("start.too_many_attempts", lang, minutes=minutes, contact=settings.organizer_contact))
        return

    result = await link_by_contact(session, _tg_user(user), normalize_phone(message.contact.phone_number))
    await _handle_result(
        message, result, via="contact", lang=lang, state=state, session=session, bot=bot, redis=redis, settings=settings
    )
