"""Редактируемые тексты: расписание, локация, контакты.

Пока админ ничего не задал, показывается текст по умолчанию из locales (content.<key>).
"""
from dataclasses import dataclass, field
from typing import Any

from aiogram import Bot
from aiogram.types import ReplyKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import ContentPage
from app.i18n import t

PAGES = ("schedule", "location", "contacts")
# Nazarbayev University, Кабанбай батыра 53, Астана.
DEFAULT_LOCATION = (51.0905, 71.3985)


@dataclass
class Page:
    text_html: str
    latitude: float | None = None
    longitude: float | None = None
    media_group: list[dict[str, Any]] = field(default_factory=list)


async def get_page(session: AsyncSession, settings: Settings, key: str, lang: str) -> Page:
    row = await session.get(ContentPage, (key, lang))
    if row is None and lang != "ru":
        row = await session.get(ContentPage, (key, "ru"))
    if row is not None:
        return Page(row.text_html, row.latitude, row.longitude, list(row.media_group or []))

    text = t(f"content.{key}", lang, contact=settings.organizer_contact, channel=settings.channel_link)
    if key == "location":
        return Page(text, *DEFAULT_LOCATION)
    return Page(text)


CAPTION_LIMIT = 1024


async def send_page(
    bot: Bot, chat_id: int, text: str, media: list[dict[str, Any]], reply_markup: ReplyKeyboardMarkup | None = None
) -> None:
    """Страница: картинки (одна или альбомом) с текстом в подписи; длинный текст идёт отдельным сообщением.

    Картинки хранит Telegram, бот отправляет только их file_id — сервер это почти не нагружает.
    """
    from app.services.broadcast import ALBUM_MEDIA

    if not media:
        await bot.send_message(chat_id, text or "—", reply_markup=reply_markup)
        return
    caption = text if text and len(text) <= CAPTION_LIMIT else None
    if len(media) == 1:
        item = media[0]
        send = {"photo": bot.send_photo, "video": bot.send_video, "document": bot.send_document}[item["type"]]
        await send(chat_id, item["file_id"], caption=caption, reply_markup=reply_markup)
    else:
        await bot.send_media_group(
            chat_id, [ALBUM_MEDIA[i["type"]](media=i["file_id"], caption=caption if n == 0 else None) for n, i in enumerate(media)]
        )
    if text and caption is None:
        await bot.send_message(chat_id, text, reply_markup=reply_markup)
