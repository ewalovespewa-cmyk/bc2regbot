"""Общий сценарий самочекина: /start ci_<код> из QR, сканер в Mini App и отложенный чекин после привязки."""
import html
import time

from aiogram import Bot
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.keyboards import main_menu
from app.config import Settings
from app.db.models import EventDay, Participant
from app.i18n import t
from app.services import checkin_codes, ratelimit, security
from app.services.checkin import CheckinResult, CheckinStatus, checkin_pending, day_number, is_open, self_checkin, today_day
from app.services.importer import team_display_name

INVALID_LIMIT = (10, 600)  # неверных кодов за 10 минут
PENDING_TTL = 600  # сколько живёт отложенный чекин, пока человек привязывает аккаунт


def bingo_keyboard(settings: Settings, lang: str) -> InlineKeyboardMarkup | None:
    if not settings.games_bot_username:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=t("checkin.open_bingo", lang), url=f"https://t.me/{settings.games_bot_username}")]]
    )


def call_organizer_keyboard(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=t("checkin.call_organizer", lang), callback_data="ci:help")]]
    )


async def reply_result(message: Message, result: CheckinResult, participant: Participant, session: AsyncSession, settings: Settings) -> None:
    lang = participant.lang
    if result.status in (CheckinStatus.OK, CheckinStatus.ALREADY):
        days = (await session.scalars(select(EventDay))).all()
        n = day_number(result.day, list(days))
        if result.status == CheckinStatus.ALREADY:
            from zoneinfo import ZoneInfo

            at = result.checkin.created_at.astimezone(ZoneInfo(settings.timezone)).strftime("%H:%M")
            await message.answer(t("checkin.already", lang, time=at), reply_markup=main_menu(lang))
            return
        team = participant.team
        if team is None or team.is_solo:
            text = t("checkin.ok_solo", lang, day=n)
        else:
            text = t(
                "checkin.ok_team", lang, day=n, team=html.escape(team_display_name(team, lang)),
                present=result.team_present, total=result.team_total,
            )
        await message.answer(text, reply_markup=main_menu(lang))
        if bingo := bingo_keyboard(settings, lang):
            await message.answer(t("checkin.bingo", lang), reply_markup=bingo)
        return
    if result.status == CheckinStatus.CLOSED:
        await message.answer(t("checkin.closed", lang), reply_markup=main_menu(lang))
        return
    await message.answer(t("checkin.expired", lang), reply_markup=main_menu(lang))


async def _too_many_invalid(message: Message, session, bot, redis, settings, lang: str) -> bool:
    key = f"badcode:{message.from_user.id}"
    if await ratelimit.hit(redis, key, *INVALID_LIMIT):
        return False
    await message.answer(t("checkin.too_many", lang), reply_markup=call_organizer_keyboard(lang))
    await security.report(
        session, bot=bot, redis=redis, settings=settings, kind=security.INVALID_CODE_FLOOD,
        telegram_user_id=message.from_user.id, username=message.from_user.username,
    )
    return True


async def handle_code(
    message: Message,
    code: str | None,
    *,
    state: FSMContext,
    session: AsyncSession,
    bot: Bot,
    redis: Redis,
    settings: Settings,
    participant: Participant | None,
    lang: str,
) -> None:
    if participant is None:
        # Не привязан: проверяем код сейчас, запоминаем чекин и привязываем (по username или контакту).
        from app.bot.handlers.start import link_or_ask_contact

        day = await today_day(session, settings)
        if not is_open(day):
            await message.answer(t("checkin.closed", lang))
            return
        if not code or not checkin_codes.is_valid(settings.checkin_secret, day.id, code):
            if not await _too_many_invalid(message, session, bot, redis, settings, lang):
                await message.answer(t("checkin.expired", lang))
            return
        await state.update_data(pending_checkin={"day_id": day.id, "at": time.time()})
        await link_or_ask_contact(message, state=state, session=session, bot=bot, redis=redis, settings=settings, lang=lang)
        if await state.get_state() is not None:  # username не совпал — ждём контакт
            await message.answer(t("checkin.need_link", lang))
        return

    result = await self_checkin(session, participant, code or "", settings)
    if result.status == CheckinStatus.INVALID_CODE and await _too_many_invalid(message, session, bot, redis, settings, lang):
        return
    await reply_result(message, result, participant, session, settings)


async def finish_pending(
    message: Message, pending: dict | None, participant: Participant, session: AsyncSession, settings: Settings
) -> None:
    """Вызывается сразу после успешной привязки: засчитываем чекин, если QR сканировали недавно."""
    if not pending or time.time() - float(pending.get("at", 0)) > PENDING_TTL:
        return
    result = await checkin_pending(session, participant, int(pending["day_id"]), settings)
    await reply_result(message, result, participant, session, settings)
