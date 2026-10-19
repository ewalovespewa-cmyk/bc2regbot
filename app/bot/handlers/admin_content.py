"""/content: админ редактирует расписание, локацию, FAQ и контакты прямо в боте.

Новый текст присылается обычным сообщением — форматирование Telegram (жирный, ссылки…)
сохраняется. Перед сохранением бот показывает предпросмотр. Для локации можно прислать
геоточку (📎 → Геопозиция).
"""
import html

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.filters import RoleFilter
from app.config import Settings
from app.db.models import ContentPage, Role
from app.services.audit import log_admin_action
from app.services.content import PAGES, get_page

router = Router(name="admin_content")
router.message.filter(F.chat.type == "private")

TITLES = {"schedule": "🗓 Расписание", "location": "📍 Локация", "faq": "❓ FAQ", "contacts": "☎️ Контакты"}
LANG_FLAGS = {"ru": "RU", "en": "EN"}
MAX_TEXT = 4000


class ContentStates(StatesGroup):
    waiting_text = State()


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def pages_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [_btn(f"{TITLES[key]} {LANG_FLAGS[lang]}", f"c:edit:{key}:{lang}") for lang in LANG_FLAGS] for key in PAGES
        ]
    )


@router.message(Command("content"), RoleFilter(Role.ADMIN))
async def cmd_content(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("📝 Какую страницу редактируем?", reply_markup=pages_keyboard())


@router.callback_query(F.data.startswith("c:edit:"), RoleFilter(Role.ADMIN))
async def on_edit(callback: CallbackQuery, state: FSMContext, session: AsyncSession, settings: Settings) -> None:
    _, _, key, lang = callback.data.split(":")
    page = await get_page(session, settings, key, lang)
    custom = await session.get(ContentPage, (key, lang))
    await state.set_state(ContentStates.waiting_text)
    await state.update_data(key=key, lang=lang, text=None, lat=page.latitude, lon=page.longitude)

    origin = "задан админом" if custom else "текст по умолчанию"
    extra = "\nДля локации можно также прислать геоточку (📎 → Геопозиция)." if key == "location" else ""
    await callback.message.answer(f"Сейчас ({origin}):")
    await callback.message.answer(page.text_html)
    buttons = [[_btn("Отмена", "c:cancel")]]
    if custom:
        buttons.insert(0, [_btn("↩️ Вернуть текст по умолчанию", f"c:reset:{key}:{lang}")])
    await callback.message.answer(
        f"Пришли новый текст для «{TITLES[key]}» ({LANG_FLAGS[lang]}) одним сообщением — форматирование сохранится.{extra}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )
    await callback.answer()


@router.message(ContentStates.waiting_text, F.location, RoleFilter(Role.ADMIN))
async def on_location(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    if data.get("key") != "location":
        await message.answer("Геоточка нужна только для страницы «Локация». Пришли текст.")
        return
    await state.update_data(lat=message.location.latitude, lon=message.location.longitude)
    await message.answer("📍 Точка обновлена. Пришли текст или нажми «Сохранить».", reply_markup=_confirm_keyboard())


@router.message(ContentStates.waiting_text, F.text, ~F.text.startswith("/"), RoleFilter(Role.ADMIN))
async def on_text(message: Message, state: FSMContext) -> None:
    text = message.html_text
    if len(text) > MAX_TEXT:
        await message.answer(f"Слишком длинно ({len(text)} символов), максимум {MAX_TEXT}.")
        return
    await state.update_data(text=text)
    await message.answer("👀 <b>Предпросмотр:</b>")
    await message.answer(text)
    await message.answer("Сохранить?", reply_markup=_confirm_keyboard())


def _confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[_btn("✅ Сохранить", "c:save"), _btn("Отмена", "c:cancel")]])


@router.callback_query(F.data == "c:save", ContentStates.waiting_text, RoleFilter(Role.ADMIN))
async def on_save(callback: CallbackQuery, state: FSMContext, session: AsyncSession, settings: Settings) -> None:
    data = await state.get_data()
    key, lang = data["key"], data["lang"]
    text = data.get("text") or (await get_page(session, settings, key, lang)).text_html
    values = dict(
        key=key,
        lang=lang,
        text_html=text,
        latitude=data.get("lat") if key == "location" else None,
        longitude=data.get("lon") if key == "location" else None,
        updated_by=callback.from_user.id,
    )
    stmt = insert(ContentPage).values(**values)
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=[ContentPage.key, ContentPage.lang],
            set_={k: stmt.excluded[k] for k in ("text_html", "latitude", "longitude", "updated_by")},
        )
    )
    await log_admin_action(session, callback.from_user.id, "edit_content", f"{key}:{lang}")
    await state.clear()
    await callback.message.edit_text(f"✅ «{html.escape(TITLES[key])}» ({LANG_FLAGS[lang]}) сохранено. Участники уже видят новую версию.")
    await callback.answer()


@router.callback_query(F.data.startswith("c:reset:"), RoleFilter(Role.ADMIN))
async def on_reset(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    _, _, key, lang = callback.data.split(":")
    row = await session.get(ContentPage, (key, lang))
    if row is not None:
        await session.delete(row)
        await log_admin_action(session, callback.from_user.id, "reset_content", f"{key}:{lang}")
    await state.clear()
    await callback.message.edit_text(f"↩️ «{html.escape(TITLES[key])}» ({LANG_FLAGS[lang]}): вернул текст по умолчанию.")
    await callback.answer()


@router.callback_query(F.data == "c:cancel")
async def on_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text("Отменено, ничего не изменилось.")
    await callback.answer()
