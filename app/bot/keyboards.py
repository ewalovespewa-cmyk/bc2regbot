from aiogram import F
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    WebAppInfo,
)

from app.config import Settings
from app.i18n import SUPPORTED_LANGS, t

MENU_ITEMS = ("status", "checkin", "schedule", "location", "faq", "contacts", "ask", "language")

# Адрес Mini App со сканером QR. Задаётся при сборке диспетчера, если у бота есть HTTPS-адрес.
WEBAPP_URL: str | None = None


def menu_text(key: str):
    """Фильтр по тексту кнопки меню на любом из языков (у человека мог смениться язык)."""
    return F.text.in_({t(f"menu.{key}", lang) for lang in SUPPORTED_LANGS})


def main_menu(lang: str) -> ReplyKeyboardMarkup:
    b = lambda key: KeyboardButton(text=t(f"menu.{key}", lang))  # noqa: E731
    checkin = (
        KeyboardButton(text=t("menu.checkin", lang), web_app=WebAppInfo(url=WEBAPP_URL)) if WEBAPP_URL else b("checkin")
    )
    return ReplyKeyboardMarkup(
        keyboard=[
            [checkin, b("status")],
            [b("schedule"), b("location")],
            [b("faq"), b("contacts")],
            [b("ask"), b("language")],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def contact_keyboard(lang: str) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=t("start.contact_button", lang), request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def cancel_keyboard(lang: str) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=t("question.cancel", lang))]], resize_keyboard=True, one_time_keyboard=True
    )


def games_keyboard(settings: Settings, lang: str) -> InlineKeyboardMarkup | None:
    """Кнопки «Игры» и «Общий чат» — если бот игр и чат указаны в настройках."""
    row = []
    if settings.games_bot_username:
        row.append(InlineKeyboardButton(text=t("menu.games", lang), url=f"https://t.me/{settings.games_bot_username}"))
    if settings.games_chat_link:
        row.append(InlineKeyboardButton(text=t("menu.games_chat", lang), url=settings.games_chat_link))
    return InlineKeyboardMarkup(inline_keyboard=[row]) if row else None


def answer_question_keyboard(question_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="✍️ Ответить", callback_data=f"q:answer:{question_id}")]]
    )
