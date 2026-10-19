"""Меню участника: статус, контент-страницы, язык. Последний роутер — ответ на всё непонятное."""
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import ChatMemberUpdated, Message
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.filters import Registered
from app.bot.handlers.start import ask_contact
from app.bot.keyboards import main_menu, menu_text
from app.config import Settings
from app.db.models import Participant
from app.i18n import t
from app.services.content import get_page
from app.services.status import build_status

router = Router(name="menu")
router.message.filter(F.chat.type == "private", Registered())

fallback_router = Router(name="fallback")
fallback_router.message.filter(F.chat.type == "private")


@router.message(Command("menu"))
async def cmd_menu(message: Message, participant: Participant) -> None:
    await message.answer(t("menu.title", participant.lang), reply_markup=main_menu(participant.lang))


@router.message(menu_text("status"))
@router.message(Command("status"))
async def show_status(message: Message, session: AsyncSession, settings: Settings, participant: Participant) -> None:
    text = await build_status(session, participant, participant.lang, settings.timezone)
    await message.answer(text, reply_markup=main_menu(participant.lang))


async def _show_page(message: Message, session: AsyncSession, settings: Settings, participant: Participant, key: str) -> None:
    page = await get_page(session, settings, key, participant.lang)
    await message.answer(page.text_html, reply_markup=main_menu(participant.lang))
    if page.latitude is not None and page.longitude is not None:
        await message.answer_location(page.latitude, page.longitude)


@router.message(menu_text("schedule"))
async def show_schedule(message: Message, session: AsyncSession, settings: Settings, participant: Participant) -> None:
    await _show_page(message, session, settings, participant, "schedule")


@router.message(menu_text("location"))
async def show_location(message: Message, session: AsyncSession, settings: Settings, participant: Participant) -> None:
    await _show_page(message, session, settings, participant, "location")


@router.message(menu_text("faq"))
async def show_faq(message: Message, session: AsyncSession, settings: Settings, participant: Participant) -> None:
    await _show_page(message, session, settings, participant, "faq")


@router.message(menu_text("contacts"))
async def show_contacts(message: Message, session: AsyncSession, settings: Settings, participant: Participant) -> None:
    await _show_page(message, session, settings, participant, "contacts")


@router.message(menu_text("language"))
async def switch_language(message: Message, participant: Participant) -> None:
    participant.lang = "en" if participant.lang == "ru" else "ru"
    await message.answer(t("menu.language_switched", participant.lang), reply_markup=main_menu(participant.lang))


@fallback_router.my_chat_member(F.chat.type == "private")
async def on_block_change(event: ChatMemberUpdated, session: AsyncSession) -> None:
    """Пользователь заблокировал или разблокировал бота — учитываем в рассылках."""
    blocked = event.new_chat_member.status == "kicked"
    await session.execute(
        update(Participant).where(Participant.telegram_user_id == event.from_user.id).values(is_blocked_bot=blocked)
    )


@fallback_router.message()
async def fallback(message: Message, state: FSMContext, participant: Participant | None, lang: str) -> None:
    if participant:
        await message.answer(t("menu.unknown", participant.lang), reply_markup=main_menu(participant.lang))
    else:
        await ask_contact(message, state, lang)
